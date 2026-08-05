# Madup Infra Manager Control Plane Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the authenticated Claude plugin and MIM control plane that can safely deploy approved `madupmarketing` web apps, schedule jobs, show user/admin usage and cost, and automatically quarantine or remove eligible compute.

**Architecture:** Keep policy and state transitions in a framework-independent Python domain package. A Cloudflare Access-protected Worker signs requests to a public-at-transport Cloud Run API, which verifies both the user assertion and the Worker origin signature. The public API can plan and enqueue only; separate private workers use Firestore, Cloud Tasks, Cloud Build, Cloud Run, Scheduler, Secret Manager, Monitoring, and the MIM-only billing export to perform bounded work.

**Tech Stack:** Python 3.13, FastAPI, official Python MCP SDK, PyJWT/cryptography, Google Cloud Python clients, Firestore, Cloud Tasks, Cloud Build, Cloud Run, Cloud Scheduler, Secret Manager, Cloud Monitoring, BigQuery billing export, Cloudflare Worker JavaScript, stdlib `unittest`, `ruff`, `mypy`, Docker, Bash, Claude plugin marketplace format.

---

## Execution Rules

- Execute on `main`, as explicitly requested by the user. Check for unrelated work before every task and never revert it.
- Follow TDD: add one failing behavior, observe the failure, implement the minimum, and rerun the focused and cumulative suites.
- Use Lore commit messages. Every task ends in an independently reviewable commit with `Constraint`, `Confidence`, `Scope-risk`, `Tested`, and `Not-tested` trailers where useful.
- Keep `plugins/madup-infra-manager/control-plane/bootstrap/` and its direct-IAP service isolated. Do not reuse its service account, route, auth code, image, or secrets.
- Do not expose a public mutating route until Tasks 1–13 pass and the Task 14 mutation gate is explicitly enabled.
- Production mutations target only the reviewed operator boundary carried by `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, `MIM_BILLING_ACCOUNT_ID`, and `MIM_OPERATOR_EMAIL`, plus the fixed runtime region policy.
- Do not push to the remote, modify Gabia/Cloudflare DNS, create a GitHub App, enable new GCP APIs, grant IAM, or deploy production resources until the corresponding preflight reports the exact intended diff.
- Runtime dependency additions are confined to the control-plane application and pinned in `uv.lock`; the rest of the plugin repository stays dependency-light.
- All commands below run from the repository root `/Users/madup/Documents/git/madup/claude-plugins`.
- Employees never enter GCP project, organization, billing, Cloudflare, or operator values, and they never receive a shared API key. Production employee login remains browser Cloudflare Access Managed OAuth after the release gate proves it.

## Milestone Gates

1. **Local contract:** plugin package, policy engine, local API, fake worker, and full unit suite pass without cloud credentials.
2. **Authenticated read-only staging:** Cloudflare origin protection, Firestore, and read-only MCP/dashboard pass staging canaries; no mutation route exists.
3. **Private execution staging:** Cloud Tasks and private workers can execute approved fake/canary manifests; the public service still cannot request production mutation.
4. **Bounded mutation staging:** plan-bound deploy, schedule, and secret flows pass IAM denial, rollback, redaction, quota, and lifecycle tests.
5. **Pilot production:** plugin, dashboard, identity sync, usage ledger, emergency stop, offboarding, and 30-day cleanup pass an operator-reviewed release gate.

### Task 1: Package MIM as a discoverable Claude plugin

**Files:**
- Create: `tests/test_madup_infra_manager_plugin.py`
- Create: `plugins/madup-infra-manager/.claude-plugin/plugin.json`
- Create: `plugins/madup-infra-manager/.mcp.json`
- Create: `plugins/madup-infra-manager/skills/madup-infra-manager/SKILL.md`
- Create: `plugins/madup-infra-manager/skills/madup-infra-manager/references/examples.md`
- Modify: `.claude-plugin/marketplace.json`
- Modify: `README.md`

**Step 1: Write the failing packaging tests**

Assert that the manifest and marketplace versions agree, `.mcp.json` defines one `http` server at `https://mim.madupai.com/mcp`, the skill triggers on Korean deploy/schedule/status/cost/repair phrases, the skill requires plan-first behavior and one question at a time, and the platform repo is never described as an application source.

```python
def test_mim_mcp_is_remote_and_plan_first(self):
    config = json.loads(MCP_CONFIG.read_text())
    server = config["mcpServers"]["madup-infra-manager"]
    self.assertEqual(server["type"], "http")
    self.assertEqual(server["url"], "https://mim.madupai.com/mcp")
    skill = SKILL.read_text()
    self.assertIn("계획", skill)
    self.assertIn("한 번에 하나", skill)
```

**Step 2: Run the focused test and observe failure**

Run: `python3 -m unittest tests/test_madup_infra_manager_plugin.py -v`

Expected: FAIL because the MIM plugin manifest, MCP configuration, and skill do not exist.

**Step 3: Add the minimum plugin package**

Use the existing marketplace conventions. The skill must describe only typed MIM tools and must never tell Claude to run `gcloud`, `docker`, arbitrary shell, or accept cloud credentials. Register version `0.1.0` in the manifest and marketplace.

**Step 4: Validate and rerun tests**

Run:

```bash
python3 -m unittest tests/test_madup_infra_manager_plugin.py -v
claude plugin validate .
```

Expected: tests pass and the marketplace validates.

**Step 5: Commit**

Commit intent: `Make governed infrastructure discoverable from Claude`

### Task 2: Create the isolated control-plane Python project

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/pyproject.toml`
- Create: `plugins/madup-infra-manager/control-plane/uv.lock`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/__init__.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/config.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/__init__.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_config.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/fakes.py`

**Step 1: Write failing configuration tests**

Require exact project, organization, region, allowed GitHub owner, Cloudflare issuer/audience, identity staleness window, plan expiry, quota defaults, and lifecycle windows. Reject unknown environment keys, unsafe URLs, wrong projects, and relaxed caps.

```python
def test_production_config_rejects_wrong_project(self):
    env = valid_env() | {"MIM_PROJECT_ID": "sensitive-production"}
    with self.assertRaises(ConfigError):
        Settings.from_mapping(env)
```

**Step 2: Run and observe failure**

Run: `uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_config.py -v`

Expected: FAIL because the project and package do not exist.

**Step 3: Add pinned application dependencies and fail-closed settings**

Pin the resolved dependency graph in `uv.lock`. Keep runtime dependencies limited to FastAPI/Uvicorn, the official MCP SDK, PyJWT crypto support, HTTP client, safe YAML parsing, and the Google clients used by later adapters. Keep `ruff` and `mypy` in the development group.

Implement immutable `Settings` with no implicit use of ambient project selection. Production mode must compare configured identifiers to the approved constants.

**Step 4: Verify the isolated project**

Run:

```bash
uv sync --project plugins/madup-infra-manager/control-plane --all-groups
uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'
uv run --project plugins/madup-infra-manager/control-plane ruff check plugins/madup-infra-manager/control-plane
uv run --project plugins/madup-infra-manager/control-plane mypy plugins/madup-infra-manager/control-plane/src
```

Expected: all commands pass.

**Step 5: Commit**

Commit intent: `Keep control-plane dependencies and configuration inside a closed boundary`

### Task 3: Define domain records, state machines, and store contracts

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/domain/models.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/domain/states.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/ports/store.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/memory_store.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_models.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_operations_idempotency.py`

**Step 1: Write failing model and transition tests**

Cover `User`, `RepositoryAdmission`, `Workload`, `DeploymentPlan`, `Operation`, `Schedule`, `SecretMetadata`, `UsageEntry`, `ActivityEvent`, `DailyUsageAggregate`, `AuditEvent`, `LifecycleAction`, and `OriginRequestClaim`. Lock operation transitions and create-only idempotency.

```python
def test_operation_cannot_skip_from_queued_to_succeeded(self):
    operation = operation_in(OperationState.QUEUED)
    with self.assertRaises(InvalidTransition):
        operation.transition(OperationState.SUCCEEDED)
```

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_models.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_operations_idempotency.py -v
```

Expected: FAIL for missing domain records.

**Step 3: Implement immutable records and a memory store**

Use typed IDs, timezone-aware UTC timestamps, explicit enums, optimistic versions, append-only audit writes, create-only replay claims, and a store `Protocol`. The memory adapter is the reference fake for later adapter contract tests.

**Step 4: Run focused and cumulative suites**

Run: `uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'`

Expected: all tests pass.

**Step 5: Commit**

Commit intent: `Make MIM state transitions explicit before adding cloud effects`

### Task 4: Enforce identity, tenant authorization, and origin trust

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/security/origin.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/security/identity.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/security/authorization.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_origin_hmac.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_auth_policy.py`

**Step 1: Write failing negative tests first**

Require origin HMAC validation before JWT parsing. Reject missing signature, altered body digest, timestamp outside 60 seconds, reused request ID, wrong Cloudflare audience/issuer, non-Madup email, missing group, suspended user, and stale identity data. Test overlapping origin keys during rotation.

```python
def test_valid_user_token_cannot_bypass_worker(self):
    request = signed_user_request(origin_signature=None)
    with self.assertRaises(OriginDenied):
        authenticate(request, store=self.store, clock=self.clock)
    self.assertEqual(self.jwt_verifier.calls, 0)
```

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_origin_hmac.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_auth_policy.py -v
```

Expected: FAIL because security modules are missing.

**Step 3: Implement the ordered security pipeline**

Canonicalize method, path, raw-body SHA-256, timestamp, and request ID. Compare HMACs in constant time, atomically claim the ID, then validate the Cloudflare JWT with cached JWKS and exact claims. Finally enforce local user lifecycle, role, ownership, and identity freshness.

**Step 4: Run tests, type checking, and a secret-log assertion**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_origin_hmac.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_auth_policy.py -v
uv run --project plugins/madup-infra-manager/control-plane mypy plugins/madup-infra-manager/control-plane/src
```

Expected: all negative and rotation cases pass; signatures and tokens never appear in captured logs.

**Step 5: Commit**

Commit intent: `Require trusted edge transit in addition to a valid user identity`

### Task 5: Bind every mutation to a single-use plan and sanitize all output

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/domain/plans.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/security/redaction.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/audit.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_plan_hashing.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_redaction_audit.py`

**Step 1: Write failing plan and redaction tests**

Assert canonical plan hashing, policy-version binding, expiry, actor binding, single-use consumption, idempotency, and rejection after any material field changes. Assert removal of authorization headers, cookies, secret-like values, environment values, and source tokens from API, audit, dashboard, and Claude-visible output.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_plan_hashing.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_redaction_audit.py -v
```

Expected: FAIL for missing services.

**Step 3: Implement canonical JSON plans and allowlist redaction**

Use sorted, separator-stable JSON and SHA-256 over the complete material plan plus policy version. Store only the hash and sanitized summary in audit events; atomically consume plans with operation creation.

**Step 4: Run focused and cumulative suites**

Run: `uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'`

Expected: all tests pass.

**Step 5: Commit**

Commit intent: `Prevent conversational intent from becoming an unreviewed mutation`

### Task 6: Admit only selected immutable `madupmarketing` source

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/repository_admission.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/classifier.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/build_template.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_repo_admission.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_workload_admission.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/fixtures/repos/streamlit/app.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/fixtures/repos/streamlit/requirements.txt`
- Create: `plugins/madup-infra-manager/control-plane/tests/fixtures/repos/nextjs/package.json`
- Create: `plugins/madup-infra-manager/control-plane/tests/fixtures/repos/nextjs/package-lock.json`
- Create: `plugins/madup-infra-manager/control-plane/tests/fixtures/repos/nextjs/app/page.tsx`
- Create: `plugins/madup-infra-manager/control-plane/tests/fixtures/repos/scheduled_script/main.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/fixtures/repos/scheduled_script/mim.yaml`

**Step 1: Write failing admission tests**

Accept only selected `madupmarketing` repository IDs at immutable SHAs. Reject `madup-dct/claude-plugins`, non-Madup owners, forks, redirects, unselected repos, wrong installation IDs, mutable refs, and SHA mismatches. Reject manifests that request custom IAM, project, service account, VPC, build steps, arbitrary container, CPU/memory increases, or sub-hour schedules.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_repo_admission.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_workload_admission.py -v
```

Expected: FAIL for missing admission services.

**Step 3: Implement hybrid classification and trusted templates**

Autodetect only clear Streamlit, Next.js, and scheduled-script fixtures. Treat `mim.yaml` as a restrictive declaration, parsed with safe YAML loading and an exact key allowlist. Ambiguity produces a typed question instead of a guessed build. Generate build steps in MIM code; ignore repository workflow and build configuration files.

**Step 4: Run tests and inspect generated templates**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_repo_admission.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_workload_admission.py -v
rg -n "cloudbuild.yaml|terraform|serviceAccount|vpc" plugins/madup-infra-manager/control-plane/src/mim_control_plane/services
```

Expected: tests pass and no repository-controlled infrastructure path exists.

**Step 5: Commit**

Commit intent: `Keep platform releases and user workload source on separate trust paths`

### Task 7: Implement usage attribution, user quotas, and emergency cost policy

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/usage.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/quota.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_usage_cost_policy.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_activity_metrics.py`

**Step 1: Write failing threshold and attribution tests**

Cover two services, three schedules, five secrets, KRW 1,000 default, KRW 10,000 administrative ceiling, reservation for billing lag, direct workload attribution, shared platform bucket, 70% warning, 90% admission block, projected-limit pause, and the organization-wide emergency stop. Cover authenticated active-user windows, unique dashboard visitors, MCP actions, deployments, schedule runs, outcomes, policy denials, latency buckets, per-user scope, daily rollups, and detailed-event expiry. Assert that raw prompts, request bodies, client IPs, user agents, cookies, authorization headers, and secret-like values cannot enter an activity record.

```python
def test_shared_platform_cost_does_not_consume_user_limit(self):
    state = ledger(user_direct=900, platform_shared=50_000)
    self.assertEqual(policy.user_percent(state, USER), 90)
    self.assertTrue(policy.emergency_stop_required(state))
```

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_usage_cost_policy.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_activity_metrics.py -v
```

Expected: FAIL for missing usage and quota services.

**Step 3: Implement deterministic policy decisions**

Keep estimates and finalized costs separate with timestamp and confidence. Return policy decisions as data (`warn`, `block_new`, `pause`, `emergency_stop`) so workers, API, and dashboard use the same logic. Record only normalized authenticated activity metadata, compute 24-hour/7-day/30-day active-user counts and daily aggregates deterministically, and expire detailed activity independently from append-only security audit events.

**Step 4: Run focused and cumulative suites**

Run: `uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'`

Expected: all tests pass.

**Step 5: Commit**

Commit intent: `Make delayed billing signals subordinate to immediate policy limits`

### Task 8: Implement schedule, offboarding, inactivity, and repair policy

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/schedules.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/lifecycle.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/repair.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_scheduler_gate.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_lifecycle_offboarding.py`

**Step 1: Write failing lifecycle tests**

Test hourly `Asia/Seoul` normalization, minimum one-hour cadence, single lease, three-failure disablement, immediate offboarding quarantine, seven-day transfer, 23-day warning, 30-day compute cleanup, reactivation cancellation, legal/admin hold, and a stale cleanup message that cannot delete a reactivated workload.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_scheduler_gate.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_lifecycle_offboarding.py -v
```

Expected: FAIL for missing lifecycle services.

**Step 3: Implement pure eligibility and action planning**

Lifecycle code returns proposed actions; it never deletes directly. Automatic deletion can select only reproducible Cloud Run service/job, Scheduler, and later unreferenced images. It must never select source, audit, secrets, or persistent data.

**Step 4: Run tests with boundary clocks**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_scheduler_gate.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_lifecycle_offboarding.py -v
```

Expected: exact 7-, 23-, and 30-day boundary cases pass.

**Step 5: Commit**

Commit intent: `Turn offboarding and inactivity into recoverable compute cleanup`

### Task 9: Render bounded desired state and build a private fake execution plane

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/render.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/deploy.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/schedule_gateway.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/reconcile.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_deploy_plan.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_private_workers.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_reconcile_drift.py`

**Step 1: Write failing renderer and worker tests**

Lock min/max instances, CPU, memory, concurrency, timeouts, job task count, parallelism, retries, runtime identity, exact image digest, labels, ingress/IAP, and secret attachments. Streamlit and Next.js services must render `ingress=all` plus direct Cloud Run IAP enabled, while Jobs and private workers remain machine-only; no workload may render `allUsers`, project-level IAP access, or a caller-selected IAM principal. Reject mutable tags and any manifest not signed by the internal renderer. Ensure public-service fakes can enqueue but cannot call cloud mutation ports.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_deploy_plan.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_private_workers.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_reconcile_drift.py -v
```

Expected: FAIL for missing renderers and workers.

**Step 3: Implement ports and fake adapters**

Define narrow ports for task enqueueing, build, Artifact Registry, Cloud Run, Scheduler, Secret Manager, and IAM. Implement deterministic fakes and worker orchestration through `queued → building → deploying → verifying → succeeded`, with rollback on health failure.

**Step 4: Run an in-process vertical flow**

Run: `uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_private_workers.py -v`

Expected: plan → consume → enqueue → fake build → fake deploy → verify succeeds once; duplicate delivery returns the same operation.

**Step 5: Commit**

Commit intent: `Keep infrastructure effects behind private manifest-validated workers`

### Task 10: Expose a read-only API, MCP tools, and role-filtered dashboard

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/api.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/mcp.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/dashboard.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/static/index.html`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/static/app.js`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/static/styles.css`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_api_readonly.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_mcp_contract.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_dashboard_views.py`

**Step 1: Write failing transport contracts**

Require `/healthz`, read-only deploy/schedule planning, operation status, workload list, usage, cost, activity summary, and sanitized failure endpoints. Require MCP tools `plan_deploy`, `plan_schedule`, `get_operation`, `list_workloads`, `get_usage`, and `explain_failure`. Assert that no mutating tool or API route exists yet. User fixtures must see only their own authenticated dashboard visits, Claude/MCP actions, deployments, schedule executions, outcomes, and quota/cost use. Admin fixtures must see 24-hour/7-day/30-day active-user counts, unique authenticated dashboard visitors, MCP usage, deployments, builds, schedule success, failures, denials, and platform overhead without access to non-MIM projects.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_api_readonly.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_mcp_contract.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_dashboard_views.py -v
```

Expected: FAIL for missing transports.

**Step 3: Implement thin transports over domain services**

Authenticate every non-health request, enforce role-scoped queries, serve a simple accessible dashboard, show estimated/finalized costs separately, show normalized dashboard and Claude/MCP usage without raw prompts or browser fingerprinting data, and never expose secret values. Return operation IDs for future long-running tools.

**Step 4: Run local server smoke and all unit tests**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'
uv run --project plugins/madup-infra-manager/control-plane uvicorn mim_control_plane.api:app --host 127.0.0.1 --port 8081
```

Expected: tests pass; authenticated fake requests show only scoped data; mutation paths return 404/405. Stop the local server after the smoke test.

**Step 5: Commit**

Commit intent: `Make MIM usage visible before enabling any cloud mutation`

### Task 11: Add the fail-closed Cloudflare edge Worker

**Files:**
- Create: `plugins/madup-infra-manager/edge/worker/package.json`
- Create: `plugins/madup-infra-manager/edge/worker/wrangler.jsonc`
- Create: `plugins/madup-infra-manager/edge/worker/src/index.js`
- Create: `plugins/madup-infra-manager/edge/worker/test/index.test.js`
- Create: `plugins/madup-infra-manager/edge/worker/README.md`

**Step 1: Write failing Node tests**

Test method/path/body canonicalization, HMAC headers, unique request IDs, no forwarding without Cloudflare identity context, stripping caller-supplied origin headers, fail-closed origin errors, WebSocket/stream preservation, and absence of secrets in logs.

**Step 2: Run and observe failure**

Run: `npm --prefix plugins/madup-infra-manager/edge/worker test`

Expected: FAIL because the Worker does not exist.

**Step 3: Implement the minimum proxy**

Use Web Crypto HMAC and a Worker secret binding. Construct fresh origin headers after removing all caller values. Forward only the allowed MIM methods and paths to the configured generated Cloud Run origin. Do not proxy arbitrary hostnames or URLs.

**Step 4: Run Worker tests and dry-run validation**

Run:

```bash
npm --prefix plugins/madup-infra-manager/edge/worker test
npm --prefix plugins/madup-infra-manager/edge/worker exec wrangler -- deploy --dry-run
```

Expected: tests and dry run pass; no deployment occurs.

**Step 5: Commit**

Commit intent: `Make the public MIM hostname a signed edge rather than an origin bypass`

### Task 12: Add Firestore and Cloud Tasks adapters with contract tests

**Implementation checkpoint (2026-08-02):** The authoritative Google Directory
identity slice is implemented ahead of the general store adapter. It uses the
explicit MIM project, the fixed Firestore `(default)` database, and Cloud Run
compute-metadata credentials only; it does not discover ambient ADC or accept an
employee-supplied project, database, credential, or secret. Versioned user,
audit, and snapshot-ledger documents use derived document keys and are validated
strictly on read. A complete snapshot of at most 50 identities is committed in
one read-before-write transaction, with exact idempotent replay and recovery
when the commit succeeds but its response is lost. The generic Firestore store,
Cloud Tasks adapter, emulator/live staging proof, and Cloud Run Directory job
infrastructure remain pending and must not be described as deployed.

**Implementation checkpoint (2026-08-03):** The private Directory Job module
now composes only centrally validated settings, metadata-backed machine
credentials, the read-only Google provider, the explicit-project Firestore
repository, and the existing identity worker. It accepts no command-line cloud,
group, user, service-account, or credential override. Output is one bounded JSON
line containing aggregate counts plus the opaque SHA-256 snapshot correlation
ID; errors are generic and raw user IDs, emails, tokens, project IDs, and
exception text are never emitted. A Firestore
single-flight lease is acquired before the Directory read because timestamped
snapshots do not deduplicate overlapping Scheduler executions. The ten-minute
job claim is bounded by a fifteen-minute adapter maximum, persists only a
domain-separated token hash, checks expiry transactionally instead of trusting
TTL deletion, recovers an owned claim when a commit response is lost or retried,
and rejects a stale owner release after reacquisition. Container, IAM,
Scheduler, emulator, and live staging wiring are still pending.

Completed Directory-slice files:

- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/firestore_directory.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/directory_repository.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_firestore_directory.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/memory_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/config.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_config.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_directory_identity_sync.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/jobs/__init__.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/jobs/directory_sync.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/ports/directory.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_firestore_directory.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_directory_sync_job.py`

**Architecture checkpoint (2026-08-04):** `QueuedDeployTask` currently carries
the complete admitted source snapshot, whose policy limit is 1 MiB, while a
Cloud Tasks task itself is also limited to 1 MiB before protocol overhead. The
production adapter must therefore enqueue only a small versioned operation
reference and keep the validated task material behind durable private storage;
embedding the source snapshot in the HTTP task body is forbidden. Because the
queue port loads by operation ID while the Cloud Tasks task name is derived
from the idempotency key, the durable queue state must include both a hashed
operation lookup and a hashed idempotency index. A single Firestore document
must not be assumed to fit the admitted snapshot either: use bounded
per-file/chunk documents or a later immutable artifact port, validate the
reconstructed material hash, and fail closed before enqueueing if the selected
storage strategy cannot represent the admitted snapshot.

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/firestore_store.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/cloud_tasks.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/__init__.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_firestore_operations.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_cloud_tasks_delivery.py`

**Step 1: Write adapter contract tests**

Run the same store contract against memory and Firestore emulator/fake clients. Require transactions for plan consumption, operation creation, schedule leases, replay claims, lifecycle eligibility, and idempotency. Require Cloud Tasks OIDC audience, private worker URL, queue name, task name derived from idempotency key, and no user bearer token in payload.

**Step 2: Run and observe failure**

Run: `uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests/integration -p 'test_*.py'`

Expected: FAIL for missing adapters.

**Step 3: Implement production adapters**

Use explicit project/database/queue/location from validated settings. Serialize versioned domain documents. Configure Firestore TTL fields for plan expiry, replay claims, and short-lived leases without relying on TTL timing for authorization decisions.

**Step 4: Run integration and cumulative tests**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests/integration -p 'test_*.py'
uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'
```

Expected: all tests pass.

**Step 5: Commit**

Commit intent: `Persist single-use plans and private work delivery atomically`

### Task 13: Add least-privilege GitHub, build, deploy, schedule, and secret adapters

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/github.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/cloud_build.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/cloud_run.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/cloud_scheduler.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/secret_manager.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_github_webhook.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_gcp_adapters.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_secret_policy.py`

**Step 1: Write fake-client negative tests**

Verify webhook signatures and exact repository IDs before fetch. Require immutable image digests, exact target project/region, dedicated identities, bounded manifests, exact secret-level IAM, one enabled secret version, and rejection of service-account JSON, OAuth refresh tokens, PATs, and GCP/AWS access keys. Assert build adapters cannot deploy and runtime identities cannot inherit project roles. For Streamlit and Next.js require direct Cloud Run IAP with `INGRESS_TRAFFIC_ALL`, `iap_enabled=true`, the exact IAP service agent as the only Cloud Run invoker, and exact resource-level `roles/iap.httpsResourceAccessor` bindings for the workload owner plus MIM administrators; reject `allUsers`, project-level access, missing owner access, or extra principals. Require Scheduler-to-private-HTTP-gateway calls to use OIDC with the exact gateway audience; if a separate adapter ever targets the Google `run.googleapis.com/.../jobs/...:run` API directly, require an OAuth access token instead and reject OIDC for that Google API target.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_github_webhook.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_gcp_adapters.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_secret_policy.py -v
```

Expected: FAIL for missing adapters.

**Step 3: Implement narrow official-client adapters**

Pass fully qualified project/location/resource names on every call. Fetch source only through the approved GitHub installation/Cloud Build connection. Generate builds from trusted templates and return image digests. Make IAM operations resource-scoped and exact-set audited. Use Cloud Run's direct IAP integration for user-facing services so the generated `run.app` link presents Google login without a load balancer; fail the deployment health gate until IAP enablement, the IAP service-agent invoker binding, and exact owner/admin IAP access policy are all verified. This path follows the 2026 GA direct-IAP contract and is distinct from Cloudflare Access on the MIM control plane.

**Step 4: Run focused, static, and secret scans**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_github_webhook.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_gcp_adapters.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_secret_policy.py -v
uv run --project plugins/madup-infra-manager/control-plane ruff check plugins/madup-infra-manager/control-plane
uv run --project plugins/madup-infra-manager/control-plane mypy plugins/madup-infra-manager/control-plane/src
rg -n --hidden -g '!**/.git/**' -g '!uv.lock' '(BEGIN (RSA |EC )?PRIVATE KEY|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[0-9A-Za-z]{20,}|AKIA[0-9A-Z]{16})' plugins/madup-infra-manager
```

Expected: tests and static checks pass; secret scan has no real credential hits.

**Step 5: Commit**

Commit intent: `Separate source, build, deploy, schedule, and runtime authority`

### Task 14: Enable plan-bound deployment and verified automatic redeploy

**Files:**
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/api.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/mcp.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/deployments.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_deploy_rollback_flow.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_auto_deploy_flow.py`

**Step 1: Write failing end-to-end mutation tests**

Require valid actor-bound plan ID/hash, expiry, current policy, quota, single-use consumption, idempotency, queue handoff, exact SHA, build digest, authenticated health verification, traffic change, rollback, and audit. For auto-deploy, require verified webhook, enabled workload setting, approved default branch, new SHA, and complete revalidation.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_deploy_rollback_flow.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_auto_deploy_flow.py -v
```

Expected: FAIL because public mutation remains disabled.

**Step 3: Add only the deploy mutation tool and route**

Expose `deploy_from_plan` as destructive MCP metadata and `POST /v1/deployments`. The handler may atomically create an operation and enqueue a task only. It cannot call build, run, IAM, or secret APIs directly. Add GitHub webhook handling on a distinct signed route.

**Step 4: Run the mutation gate suite**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_deploy_rollback_flow.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_auto_deploy_flow.py -v
uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'
```

Expected: all tests pass; direct mutations without a plan remain denied.

**Step 5: Commit**

Commit intent: `Allow only confirmed immutable deployments through the private queue`

### Task 15: Enable gated schedules and secret lifecycle

**Files:**
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/api.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/mcp.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/secrets.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_schedule_execution_flow.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_secret_lifecycle_flow.py`

**Step 1: Write failing schedule and secret integration tests**

Cover plan-bound creation, hourly Asia/Seoul normalization, private gateway invocation, owner/quota/cost checks per run, overlap lease, bounded retries, three-failure disablement, metadata-only secret reads, direct value handoff, rotation, seven-day old-version destruction, and exact runtime binding.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_schedule_execution_flow.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_secret_lifecycle_flow.py -v
```

Expected: FAIL for missing flows.

**Step 3: Add mutating tools through plan and queue boundaries**

Expose `create_schedule_from_plan`, `pause_schedule`, `resume_schedule`, `create_secret_metadata`, `attach_secret_from_plan`, and `rotate_secret`. Secret values use a dedicated authenticated endpoint/form that suppresses logging and never travels through Claude tool output.

**Step 4: Run full integration and redaction suites**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests/integration -p 'test_*.py'
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_redaction_audit.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_secret_policy.py -v
```

Expected: all tests pass; no secret value is observable.

**Step 5: Commit**

Commit intent: `Gate recurring execution and credentials behind current policy`

### Task 15A: Add centrally managed Slack OAuth without making Slack an identity provider

**Files:**
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/domain/models.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/ports/store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/memory_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/firestore_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/api.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/dashboard.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/slack_oauth.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/slack_oauth.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_slack_oauth_flow.py`

**Step 1: Write failing central-ownership and callback tests**

Require Google Workspace identity, current MIM-group membership, and the MIM administrator role before a Slack installation can start, and prove that a Slack token or installation can never satisfy MIM authorization. The server selects the client ID, exact HTTPS redirect URI, least scope set, and approved Slack organization/workspace. Reject employee starts, caller-supplied overrides, raw tokens, arbitrary redirects/scopes/tenants, missing or mismatched state, expired state, callback replay, and a Slack code used by a different Google administrator. The first release supports only installations initiated through the MIM-generated OAuth authorization URL; reject or report as unsupported any Slack-admin-surface installation that cannot return the MIM-generated state.

Cover both an organization-ready Enterprise Grid installation restricted to exact approved workspaces and a non-Grid fallback restricted to one exact centrally approved workspace. Require the exchanged code and credential response to pass directly to the secret port, never to Claude, API output, activity records, audit payloads, or logs. Persist only installation metadata, scopes, installer, state, revocation status, timestamps, and an opaque secret reference. Only an administrator may install, replace, revoke, or uninstall the shared company app. A normal employee can only use the already-installed integration under MIM policy or request administrator setup; no employee-facing route accepts Slack configuration or credentials. Offboarding removes the employee's MIM-side use grant without uninstalling the shared app. The one shared installation secret belongs to the platform bucket, not a user's five-secret allowance.

```python
def test_slack_install_never_replaces_google_workspace_authorization(self):
    linked_slack_user = linked_user(google_group_active=False)
    self.assertFalse(access_policy.can_use_mim(linked_slack_user))
```

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_slack_oauth_flow.py -v
```

Expected: FAIL because the Slack connection registry, callback service, and adapter do not exist.

**Step 3: Implement a confidential, metadata-only Slack connection flow**

Create an expiring, single-use OAuth state bound to the authenticated Google administrator and the centrally configured tenant. Exchange the short-lived code server-side with the confidential client secret held behind Secret Manager, validate the exact returned enterprise/team identifiers and granted scopes, write token material directly to a dedicated Secret Manager resource, and save metadata transactionally only after the secret write succeeds. If the metadata commit fails, revoke the Slack credential and destroy the just-created secret version before returning a generic failure. The browser receives a generic connected/denied result; MCP and Claude receive metadata only.

Do not enable Slack's public-client PKCE mode in the first release: this control plane is a confidential server-side client, and Slack documents that enabling public-client behavior is a one-way application change with a different refresh-token lifecycle. Continue to require PKCE S256 for the separate Cloudflare Managed OAuth employee-login POC. Document the administrator browser steps for organization/workspace approval; do not pretend those Slack and Google Workspace consent actions can be automated.

**Step 4: Run focused, redaction, and authorization suites**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_slack_oauth_flow.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_auth_policy.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/test_redaction_audit.py -v
```

Expected: all tests pass; no employee-facing path starts a shared installation or accepts operator configuration or credential material, Slack alone authorizes nothing, and no Slack credential is observable.

**Step 5: Commit**

Commit intent: `Keep optional Slack consent behind mandatory company identity`

### Task 16: Add identity sync, usage ingestion, lifecycle execution, and reconciliation

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/identity_sync.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/usage_ingest.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/lifecycle.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/reconcile.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_offboarding_cleanup_flow.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_inactivity_cleanup_flow.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/integration/test_usage_ingest_flow.py`

**Step 1: Write failing worker integration tests**

Cover read-only group synchronization, immediate offboarding quarantine, session denial, schedule/access/secret-binding removal, employee Slack use-grant removal without uninstalling the shared company app, transfer, seven-day compute cleanup, 23/30-day inactivity actions, image retention, direct-cost ingestion, platform bucket, delayed finalized billing, normalized activity-event ingestion, daily usage rollups, detailed-event expiry, 70/90/limit actions, emergency stop, and safe-vs-privilege drift handling.

**Step 2: Run and observe failure**

Run:

```bash
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_offboarding_cleanup_flow.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_inactivity_cleanup_flow.py -v
uv run --project plugins/madup-infra-manager/control-plane python plugins/madup-infra-manager/control-plane/tests/integration/test_usage_ingest_flow.py -v
```

Expected: FAIL for missing workers.

**Step 3: Implement idempotent workers over pure policy**

Workers re-read current versioned state immediately before every external effect. Lifecycle delete calls carry the expected workload version and become no-ops after reactivation. Immediate offboarding/quarantine removes the workload owner's resource-level IAP access before schedule or compute cleanup; reactivation must pass the same exact-policy planner before restoring access. Usage ingestion never queries internal datasets; it reads only the configured MIM billing-export dataset and MIM-labeled resources.

**Step 4: Run full integration suite**

Run: `uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests/integration -p 'test_*.py'`

Expected: all integration tests pass.

**Step 5: Commit**

Commit intent: `Keep identity, cost, and cleanup decisions current and reversible`

### Task 17: Provision the production control-plane boundary with fail-closed scripts

**Files:**
- Create: `plugins/madup-infra-manager/infra/control-plane/config.example.env`
- Create: `plugins/madup-infra-manager/infra/control-plane/.gitignore`
- Create: `plugins/madup-infra-manager/infra/control-plane/config_lib.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/prepare_config.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/review_config.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/preflight.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/apply.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/audit_iam.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/test_prepare_config.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/test_preflight.sh`
- Create: `plugins/madup-infra-manager/infra/control-plane/test_apply.sh`
- Create: `plugins/madup-infra-manager/control-plane/Dockerfile`
- Create: `plugins/madup-infra-manager/control-plane/.dockerignore`

**Step 1: Write failing shell command-contract tests**

Require the reviewed operator boundary from `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`, and reject other targets. Assert that every operational preflight, review, plan, apply, and audit script reads `MIM_CONFIG_FILE`, defaulting only to the ignored sibling `config.env`, and rejects positional or unrecognized config flags. `prepare_config.sh` alone accepts `--output <exact-file>`, writes a new mode-`0600` file, and refuses to overwrite it. Assert separate control-plane, deploy-worker, build, schedule-gateway, identity-sync, release, and runtime identities; exact APIs including `iap.googleapis.com`; Firestore; Tasks; Artifact Registry; Secret Manager keys; private workers; `min=0/max=1`; no project-wide invoker or project-level IAP accessor; no runtime project roles; and no connection to sensitive projects. Require `apply.sh --plan --out <exact-file>` to write a redacted versioned JSON plan plus SHA-256 sidecar under the ignored `.state/` directory. Require mutation as `apply.sh --apply --plan-file <exact-file>` only, with a 30-minute maximum age, hash verification, repeated discovery, and drift rejection.

**Step 2: Run and observe failure**

Run:

```bash
bash plugins/madup-infra-manager/infra/control-plane/test_preflight.sh
bash plugins/madup-infra-manager/infra/control-plane/test_apply.sh
bash plugins/madup-infra-manager/infra/control-plane/test_prepare_config.sh
```

Expected: FAIL because the scripts do not exist.

**Step 3: Implement inspect-first, exact-diff provisioning**

Reuse the established `MIM_CONFIG_FILE` contract and safe parsing patterns from `infra/domain` without sourcing config. Separate bootstrap and production resources. Every create/update command includes explicit project and region. IAM audits reject unexpected bindings before and after apply. Default mode performs read-only checks and prints a redacted preview. `--plan --out <exact-file>` persists the immutable reviewed plan and hash; `--apply --plan-file <exact-file>` is the only mutation mode and repeats discovery before changing anything.

`prepare_config.sh` creates `config.env` from the committed example, carries forward only `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID` as public operator placeholders, leaves explicit blockers for Cloudflare account/zone/team identifiers, the selected GitHub repository IDs, and the centrally approved Slack app/organization/workspace identifiers, sets mode `0600`, and refuses to overwrite an existing file. `review_config.sh` validates every non-secret field and prints a redacted summary. Tokens, Slack client secrets, and OAuth credentials are supplied only through authenticated CLIs, OS keychains, or Secret Manager bootstrap at apply time.

Ignore `config.env` in the new directory. Commit only `config.example.env`; never commit account tokens, GitHub private keys, Cloudflare API tokens, HMAC origin keys, or Secret Manager payloads.

**Step 4: Run all local infra and container checks**

Run:

```bash
bash plugins/madup-infra-manager/infra/control-plane/test_preflight.sh
bash plugins/madup-infra-manager/infra/control-plane/test_apply.sh
bash plugins/madup-infra-manager/infra/control-plane/test_prepare_config.sh
bash plugins/madup-infra-manager/infra/domain/test_preflight.sh
bash plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh
bash -n plugins/madup-infra-manager/infra/control-plane/*.sh
docker build --platform=linux/amd64 -t mim-control-plane:test plugins/madup-infra-manager/control-plane
```

Expected: all tests and build pass without live cloud mutation.

**Step 5: Commit**

Commit intent: `Make production provisioning prove every MIM identity boundary`

### Task 18: Add reviewed Cloudflare, GitHub, and GCP release plan/apply paths

**Files:**
- Create: `plugins/madup-infra-manager/infra/edge/preflight.sh`
- Create: `plugins/madup-infra-manager/infra/edge/plan.sh`
- Create: `plugins/madup-infra-manager/infra/edge/apply.sh`
- Create: `plugins/madup-infra-manager/infra/edge/.gitignore`
- Create: `plugins/madup-infra-manager/infra/edge/test_plan.sh`
- Create: `plugins/madup-infra-manager/infra/edge/test_apply.sh`
- Create: `plugins/madup-infra-manager/infra/github/preflight.sh`
- Create: `plugins/madup-infra-manager/infra/github/plan_connection.sh`
- Create: `plugins/madup-infra-manager/infra/github/apply_connection.sh`
- Create: `plugins/madup-infra-manager/infra/github/.gitignore`
- Create: `plugins/madup-infra-manager/infra/github/test_plan_connection.sh`
- Create: `plugins/madup-infra-manager/infra/github/test_apply_connection.sh`
- Create: `plugins/madup-infra-manager/infra/release/plan.sh`
- Create: `plugins/madup-infra-manager/infra/release/apply.sh`
- Create: `plugins/madup-infra-manager/infra/release/.gitignore`
- Create: `plugins/madup-infra-manager/infra/release/test_plan.sh`
- Create: `plugins/madup-infra-manager/infra/release/test_apply.sh`

**Step 1: Write failing plan tests**

Require every script to use the same `MIM_CONFIG_FILE` contract as Task 17. Require Cloudflare zone/account/team discovery, 50-seat free-plan guard, `mim.madupai.com` only, Managed OAuth, Google IdP/MIM group allow policy, fail-closed Worker route, and no apex/wildcard change. Require selected `madupmarketing` repository IDs only, read-only source permissions, exact webhook target, and a release identity that cannot enter the app-deploy path. Validate the centrally registered Slack app metadata, exact redirect URI, least scope set, and exact approved Enterprise organization/workspaces or single non-Grid workspace without printing or accepting its client secret. Treat Slack organization/workspace approval and Google Workspace trust/consent as explicit administrator browser steps, never employee configuration.

Each plan script writes a redacted, versioned JSON plan and SHA-256 sidecar under its ignored `.state/` directory. Each apply script requires `--apply --plan-file <exact-file>`, rejects plans older than 30 minutes, recomputes the hash, repeats read-only discovery, and stops if current state differs from the plan.

**Step 2: Run and observe failure**

Run:

```bash
bash plugins/madup-infra-manager/infra/edge/test_plan.sh
bash plugins/madup-infra-manager/infra/edge/test_apply.sh
bash plugins/madup-infra-manager/infra/github/test_plan_connection.sh
bash plugins/madup-infra-manager/infra/github/test_apply_connection.sh
bash plugins/madup-infra-manager/infra/release/test_plan.sh
bash plugins/madup-infra-manager/infra/release/test_apply.sh
```

Expected: FAIL because preflight planners do not exist.

**Step 3: Implement redacted discovery, immutable plans, and bounded apply commands**

Scripts must never print tokens or private keys. They stop on missing account authority, unexpected existing DNS, more than 50 pilot seats, an unapproved repo selection, or a target outside the fixed boundary.

The edge apply path may create/update only the reviewed Cloudflare zone resources, Worker, `mim.madupai.com` proxied record, Access application, Managed OAuth flag, Google IdP/group allow policy, and fail-closed route. It outputs the assigned authoritative nameservers but cannot mutate Gabia. The GitHub apply path may create only the reviewed GCP connection/repository resources and webhook configuration for the exact selected `madupmarketing` IDs; interactive GitHub App installation remains an explicit browser step. The release apply path may build and deploy only the MIM platform images from the exact reviewed `madup-dct/claude-plugins` commit and can never submit that source to the user-workload path.

**Step 4: Run all plan tests and shell syntax checks**

Run:

```bash
bash plugins/madup-infra-manager/infra/edge/test_plan.sh
bash plugins/madup-infra-manager/infra/edge/test_apply.sh
bash plugins/madup-infra-manager/infra/github/test_plan_connection.sh
bash plugins/madup-infra-manager/infra/github/test_apply_connection.sh
bash plugins/madup-infra-manager/infra/release/test_plan.sh
bash plugins/madup-infra-manager/infra/release/test_apply.sh
bash -n plugins/madup-infra-manager/infra/edge/*.sh plugins/madup-infra-manager/infra/github/*.sh plugins/madup-infra-manager/infra/release/*.sh
```

Expected: all tests pass and plans are deterministic/redacted.

**Step 5: Commit**

Commit intent: `Require reviewed immutable plans for every external MIM change`

### Task 19: Add repository-wide quality and release gates

**Files:**
- Create: `tests/test_mim_release_contract.py`
- Create: `plugins/madup-infra-manager/infra/release/verify.sh`
- Modify: `README.md`

**Step 1: Write the failing release contract**

Require plugin validation, Python lint/type/unit/integration suites, shell suites, edge tests, container build, secret scan, IAM-policy diff, direct-origin denial canary, sensitive-project denial canary, authenticated read-only smoke, and an explicit `MIM_ENABLE_MUTATIONS=true` gate that defaults false.

**Step 2: Run and observe failure**

Run: `python3 -m unittest tests/test_mim_release_contract.py -v`

Expected: FAIL because the release verifier does not exist.

**Step 3: Implement the verifier**

The script runs local checks by default and requires explicit staging configuration for network canaries. It prints one summarized result per gate and exits nonzero on skipped required production checks. It never performs a deployment itself.

**Step 4: Run the complete local verification matrix**

Run:

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --local
git diff --check
git status --short
```

Expected: every local gate passes and only intended files are changed.

**Step 5: Commit**

Commit intent: `Make unsafe MIM releases mechanically impossible to approve`

### Task 20: Stage, verify, and then release the pilot

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/tests/staging/test_cloudflare_origin_canary.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/staging/test_runtime_iam_canary.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/staging/test_sensitive_project_denials.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/staging/test_direct_iap_breakglass.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/staging/test_slack_oauth_canary.py`
- Create: `plugins/madup-infra-manager/infra/release/smoke_test.sh`
- Modify: `docs/plans/2026-08-02-mim-control-plane-implementation.md` with actual evidence and deviations

**Step 1: Write staging canaries before applying infrastructure**

Canaries prove Cloudflare login, valid Worker transit, direct-origin denial with and without a valid user token, replay denial, role filtering, read-only MCP discovery, separate bootstrap IAP, exact IAM, denied sensitive-project/BigQuery/secret access, quota transitions, rollback, schedule gate, lifecycle dry run, mandatory Google authorization despite Slack linkage, exact Slack tenant/scope/redirect validation, single-use Slack callback state/code, and metadata-only Slack credential visibility. A user-workload canary separately proves direct Cloud Run IAP on the generated `run.app` URL: browser login succeeds for the exact owner/admin, another MIM user and an unauthenticated client are denied, `iap_enabled=true`, ingress is `all`, the IAP service agent is the only Cloud Run invoker, no project-level IAP accessor exists, and offboarding removes access before cleanup.

**Step 2: Prepare and review the single operator configuration**

Run:

```bash
bash plugins/madup-infra-manager/infra/control-plane/prepare_config.sh --output plugins/madup-infra-manager/infra/control-plane/config.env
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/control-plane/review_config.sh
```

`prepare_config.sh` fills only the public operator placeholders `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`. Populate the remaining non-secret Cloudflare account, zone, team, IdP/group identifiers, exact selected `madupmarketing` repository IDs, and centrally approved Slack app/organization/workspace identifiers from read-only discovery, then rerun `review_config.sh`. Keep all tokens, Slack client secrets, GitHub App private material, origin HMAC keys, and Secret Manager payloads out of this file and out of Git. The review must fail until every placeholder is resolved, every selected GitHub repository is in `madupmarketing`, the platform repository is absent from the application-source list, and Slack tenant/scope/redirect metadata matches the reviewed central app.

Before the employee login is announced as live, the remote-MCP OAuth POC gate must prove authorization server metadata discovery, RFC 8707 `resource parameter`, PKCE S256, dynamic client registration or manual client ID fallback, callback compatibility, token refresh and reuse, and group-removal/session expiry latency.

**Step 3: Run all read-only preflights and persist exact immutable plans**

Run:

```bash
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/control-plane/preflight.sh
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/control-plane/apply.sh --plan --out plugins/madup-infra-manager/infra/control-plane/.state/control-plane-plan.json
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/edge/plan.sh --out plugins/madup-infra-manager/infra/edge/.state/edge-plan.json
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/github/plan_connection.sh --out plugins/madup-infra-manager/infra/github/.state/github-plan.json
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/release/plan.sh --out plugins/madup-infra-manager/infra/release/.state/release-plan.json
```

Expected: each target matches the approved project, accounts, hostname, repos, identities, resources, and estimated shared cost; no mutation occurs.

**Step 4: Apply the reviewed plans in gated order**

Run the exact plan-bound commands within 30 minutes of the corresponding review:

```bash
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/control-plane/apply.sh --apply --plan-file plugins/madup-infra-manager/infra/control-plane/.state/control-plane-plan.json
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/release/apply.sh --apply --plan-file plugins/madup-infra-manager/infra/release/.state/release-plan.json
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/edge/apply.sh --apply --plan-file plugins/madup-infra-manager/infra/edge/.state/edge-plan.json
MIM_CONFIG_FILE=plugins/madup-infra-manager/infra/control-plane/config.env bash plugins/madup-infra-manager/infra/github/apply_connection.sh --apply --plan-file plugins/madup-infra-manager/infra/github/.state/github-plan.json
```

Apply production dependencies and private workers first. Deploy the control plane with mutations disabled. The Cloudflare apply creates the reviewed zone, Access application, Worker, and `mim.madupai.com` route but only reports Cloudflare's assigned nameservers. Before changing Gabia, export every existing `madupai.com` DNS record, compare it to the reviewed Cloudflare zone, and stop on any missing or unexpected record. Change only the authoritative nameservers to the exact values in the reviewed edge plan. Verify with `dig NS madupai.com`, confirm the apex remains unconfigured, and confirm only `mim.madupai.com` enters the Worker route.

Complete the interactive GitHub App installation in the browser only for the exact repository IDs in the reviewed configuration, then rerun the GitHub preflight and reject any extra repository. Configure the selected GitHub connection. Run read-only canaries. Enable canary-only mutations, deploy one approved harmless fixture, verify and remove it. Only then enable pilot mutation tools.

Complete the Google Workspace administrator trust/consent step. Then a MIM administrator starts the reviewed Slack OAuth URL and a Slack administrator approves the exact organization/workspace installation in that browser flow. Do not use or import a Slack-admin-surface install that bypasses MIM-generated state in the first release. Employees do not perform either operator setup. Rerun read-only identity and Slack metadata checks, prove an unapproved Slack tenant and a Slack-only identity are denied, prove the OAuth code/state cannot be replayed, and prove the credential is visible only as Secret Manager-backed metadata before enabling Slack tools.

**Step 5: Run full release verification and capture evidence**

Run:

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --staging
bash plugins/madup-infra-manager/infra/release/smoke_test.sh
python3 -m unittest discover -s plugins/madup-infra-manager/control-plane/tests/staging -p 'test_*.py'
```

Expected: every production gate passes. If any gate fails, mutations remain disabled and the previous bootstrap remains available.

**Step 6: Record evidence and commit**

Update this plan with exact revisions, image digests, URLs, IAM audit summaries, test counts, known gaps, and rollback point.

Commit intent: `Release MIM only after authenticated denial and rollback canaries pass`

## Program Completion Criteria

- `madup-infra-manager` installs from `madup-dct/claude-plugins` and authenticates through `mim.madupai.com` without shared user API keys.
- On first use, browser Cloudflare Access Managed OAuth checks Google Workspace identity plus the MIM access group, Claude stores and refreshes the OAuth token, and later calls reuse it without asking the employee for cloud values.
- Slack OAuth stays optional and separate, never becomes the primary MIM login, uses only the centrally approved Madup app and exact tenant, requires a MIM-initiated administrator browser install rather than employee setup, stores resulting tokens in Secret Manager, never pastes them into Claude, and enforces single-use callbacks, least scopes, rotation, revocation, and administrator-controlled shared installation.
- A permitted user can conversationally plan and deploy an admitted Streamlit/Next.js repo, schedule an admitted script hourly, inspect progress, view usage/cost, and request a bounded repair.
- A normal user cannot see or mutate another user's resources; the administrator can see all MIM usage and limits without access to any non-MIM account or project.
- Per-user direct cost and shared platform overhead remain separate; 70/90/projected-limit and overall emergency-stop behavior is proven.
- Offboarding quarantines immediately, denies new or renewed Access sessions after group removal, measures active-session latency, transfers for seven days, and deletes only eligible stateless compute. Inactivity warns at day 23 and deletes eligible compute at day 30, while retaining source, secrets, desired state, and audit records as specified.
- `madup-dct/claude-plugins`, non-`madupmarketing` repositories, arbitrary cloud credentials, mutable images, privilege requests, origin bypass, replay, stale plans, cross-user access, and sensitive-project access all fail closed in tests and staging.
- All unit, integration, plugin, shell, edge, container, IAM, and staging canaries pass; no known deployment-blocking error remains.
