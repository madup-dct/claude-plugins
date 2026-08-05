# MIM Security Review Corrections Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the production-blocking findings from the independent MIM security, specification, and release reviews without weakening source immutability, tenant isolation, or exact-target deployment controls.

**Architecture:** Durable deploy tasks contain only commit-pinned source attestations; the private deploy worker refetches the admitted immutable SHA and verifies the reviewed digest before build. Secret Manager access is split into exact runtime-secret payload access and per-workload metadata-only access, while direct Cloud Run breakglass principals are configured independently from platform administrators. Edge rollout remains an immutable, exact-resource plan with narrow Access applications, no broad mutation, and explicit live denial canaries.

**Tech Stack:** Python 3.13/FastAPI, Firestore, Google Cloud Run and Secret Manager, stdlib Go, Cloudflare Workers/Access, Bash/Python infrastructure contracts, unittest, Node test runner, Docker.

---

## Non-negotiable corrections

- Repository file bytes, base64 encodings, environment files, and token-like source content must never be serialized into Firestore deploy-task documents.
- The deploy task binds the exact admitted SHA, snapshot digest, file count, and byte count. A worker-side refetch mismatch is a trust failure before Cloud Build or runtime mutation.
- No legacy decoder may continue accepting a raw-snapshot task format. Read-only inventory found no deployed private task queue; any unexpected legacy document is rejected, quarantined from execution, and must be purged through a separately reviewed one-time migration.
- `mim-deploy-worker` must not retain project-level `roles/secretmanager.admin` or payload access to user/workload secrets.
- Workload-secret metadata checks use `roles/secretmanager.viewer` on each exact managed secret resource; workload runtimes retain only exact resource-level `secretAccessor` on attached secrets.
- Runtime bootstrap, desired-state signing, and GitHub App key payload access are separate exact trust-root bindings. No prefix-wide trust-root access is permitted for the deploy worker.
- `admin_members` never become direct user-application invokers. `breakglass_members` is a distinct reviewed field whose safe default is empty.
- Control-plane browser authentication validates the raw ASGI header list and rejects forbidden/duplicate credential, forwarding, Access, and origin-proof headers.
- Control routes continue to reject every percent escape; app routes continue to allow only well-formed escapes. This is an intentional boundary, not a bug to normalize away.
- Cloud Scheduler may invoke scheduled work only through the private schedule-gateway OIDC endpoint. The legacy direct Cloud Run Jobs target is not a reviewed production path.
- Cloudflare plans are no-op when exact state is already ready, block ambiguity/extras, pin exact IDs and before-state hashes, and never mutate nameservers or unrelated resources.

## Task 1: Make deploy tasks source-attestation only

**Files:**

- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/ports/execution.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/firestore_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/deployments.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/deploy.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/runtime.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_private_workers.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_cloud_build_adapter.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_runtime.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_deployment_api.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/integration/test_firestore_operations.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/integration/test_cloud_tasks_delivery.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/integration/test_auto_deploy_flow.py`

### Step 1.1: Write failing persistence tests

Add tests proving a `QueuedDeployTask` exposes only `expected_snapshot_digest`, `expected_snapshot_file_count`, and `expected_snapshot_byte_count`; its representation and serialized Firestore payload contain no path body, raw bytes, base64 snapshot array, `.env` marker, or token-like fixture value.

Run:

```bash
cd plugins/madup-infra-manager/control-plane
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.integration.test_firestore_operations \
  tests.integration.test_cloud_tasks_delivery
```

Expected: the new metadata-only assertions fail against the raw snapshot format.

### Step 1.2: Replace durable snapshot material with attestations

Keep `QueuedDeployTask.from_snapshot(...)` as an in-memory review-time factory, but discard file bytes after calculating the canonical digest/counts. Compute `material_hash` from immutable IDs, versions, SHA, snapshot attestations, idempotency key, and secret attachment references. Remove the raw `snapshot` property and all base64 task serialization/deserialization.

### Step 1.3: Refetch and verify the exact source in the worker

Inject the existing `SourceSnapshotPort` into `PrivateDeployWorker`. After durable admission/workload revalidation and before `BUILDING`, fetch the exact admitted SHA, recompute the canonical attestation, and compare it with the task. Network/source unavailability raises a sanitized retryable execution error so the machine endpoint returns non-success and Cloud Tasks may retry while the operation remains `QUEUED`; a digest/count mismatch is terminal `QUARANTINED/deploy_denied`. Neither path starts a build or runtime mutation.

Use the transient verified snapshot for classification and desired-state rendering. `BuildRequest` remains connected-repository/SHA based and carries no source bytes.

### Step 1.4: Share the hardened GitHub source adapter

Factor runtime construction so the control plane and private deploy worker receive the same selected-repository policy, installation-token provider, exact-SHA source adapter, bounded archive checks, and no-auth-forwarding codeload client. Do not add a dependency or persist an installation token.

### Step 1.5: Verify replay and immutability

Add tests for matching refetch, mismatch quarantine, retryable fetch failure with an unchanged queued operation, explicit rejection of the legacy raw-snapshot schema, exact task replay, webhook replay, and unchanged plan hashes. Run:

```bash
cd plugins/madup-infra-manager/control-plane
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_private_workers \
  tests.test_cloud_build_adapter \
  tests.test_runtime \
  tests.test_deployment_api \
  tests.integration.test_firestore_operations \
  tests.integration.test_cloud_tasks_delivery \
  tests.integration.test_auto_deploy_flow
```

Expected: PASS, with no durable raw-source field remaining in production code.

## Task 2: Split Secret Manager and breakglass trust boundaries

**Files:**

- Modify: `plugins/madup-infra-manager/infra/control-plane/iam/contract.py`
- Modify: `plugins/madup-infra-manager/infra/control-plane/test_preflight.sh`
- Modify: `plugins/madup-infra-manager/infra/control-plane/test_apply.sh`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/secret_manager.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/runtime.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/jobs/lifecycle.py`
- Modify: `plugins/madup-infra-manager/infra/runtime-bootstrap/bootstrap_contract.py`
- Modify: `plugins/madup-infra-manager/infra/runtime-bootstrap/bootstrap-input.template.json`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_secret_manager_adapter.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_runtime.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_lifecycle_job.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_cloud_run_adapter.py`
- Test: `plugins/madup-infra-manager/infra/runtime-bootstrap/test_bootstrap_contract.py`

### Step 2.1: Write failing IAM tests

Require the fixed project policy to exclude `mim-deploy-worker` from `roles/secretmanager.admin`. Require the following exact resource matrix:

- runtime-bootstrap secret: `secretAccessor` for `mim-control-plane`, `mim-deploy-worker`, `mim-schedule-gateway`, and `mim-maintenance`;
- desired-state signing secret: `secretAccessor` for `mim-control-plane` and `mim-deploy-worker`;
- GitHub App key secret: `secretAccessor` for `mim-control-plane` and `mim-deploy-worker`;
- GitHub webhook secret: `secretAccessor` only for `mim-control-plane`;
- control-plane origin/OAuth secrets: `secretAccessor` only for the exact control-plane runtime that consumes them;
- workload-managed secrets: `roles/secretmanager.viewer` for the exact deploy-worker metadata reader and `secretAccessor` only for attached workload runtime identities.

Explicitly deny deploy-worker `versions.access` on workload secrets, `setIamPolicy`, mutation roles, prefix-wide trust-root access, and another workload's secret.

### Step 2.2: Implement exact metadata-reader bindings

Teach `SecretManagerAdapter` the exact deploy-worker metadata-reader identity and include `roles/secretmanager.viewer` in each exact managed-secret IAM policy. Keep the workload runtime accessor and control-plane version-manager sets exact. Fail readback on any extra member or role.

### Step 2.3: Introduce `breakglass_members`

Add a sorted, unique, company-only `breakglass_members` bootstrap field. Missing input normalizes to an empty tuple; no fallback to `admin_members` is allowed. Wire only this field into user-service Cloud Run invoker policies and lifecycle compute readback. Keep `admin_members` limited to control-plane administration/legacy compatibility surfaces.

### Step 2.4: Verify exact runtime secret access

Prove each Python runtime that loads `mim-runtime-bootstrap` has exact resource-level accessor and no prefix-wide secret role. Prove deploy worker has exact access only to bootstrap/signing/GitHub key trust roots plus metadata-only access on attached workload secrets.

Run:

```bash
bash plugins/madup-infra-manager/infra/control-plane/test_preflight.sh
bash plugins/madup-infra-manager/infra/control-plane/test_apply.sh
cd plugins/madup-infra-manager/control-plane
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_secret_manager_adapter \
  tests.test_runtime \
  tests.test_lifecycle_job \
  tests.test_cloud_run_adapter
cd ..
python3 -m unittest infra/runtime-bootstrap/test_bootstrap_contract.py
```

Expected: PASS.

## Task 3: Close non-edge boundary drift

**Files:**

- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/api.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/deploy.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/cloud_scheduler.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/lifecycle_effects.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_api_readonly.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_private_workers.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_cloud_scheduler_adapter.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_lifecycle_effects.py`

### Step 3.1: Validate raw browser headers

Write failures for duplicate/extra `X-MIM-*`, extra `Cf-Access-*`, `Authorization`, `Proxy-Authorization`, and spoofable forwarding headers. Decode the raw ASGI header tuples using the established MCP HTTP pattern and pass them to identity/origin validation without dropping evidence.

### Step 3.2: Restore runtime failure classification

Write a `verify_health()` exception regression. Handle `ExecutionPlaneError` only in the `FAILED/deploy_failed` branch; keep hostname-binding/idempotency/invariant drift in `QUARANTINED/deploy_denied`.

### Step 3.3: Remove the direct-job scheduler path

Delete or hard-disable `ensure_direct_job_schedule()` and remove its positive production tests. Lifecycle schedule readback accepts only the reviewed schedule-gateway URL, exact OIDC service account, exact audience, and hourly contract.

### Step 3.4: Verify the focused boundary set

```bash
cd plugins/madup-infra-manager/control-plane
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_api_readonly \
  tests.test_private_workers \
  tests.test_cloud_scheduler_adapter \
  tests.test_lifecycle_effects
```

Expected: PASS.

## Task 4: Complete truthful dashboard projections

**Files:**

- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/ports/store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/memory_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/firestore_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/dashboard.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/test_dashboard_views.py`
- Test: `plugins/madup-infra-manager/control-plane/tests/integration/test_firestore_operations.py`

### Step 4.1: Add latest-operation lookup and workload health

Add an exact owner/workload-scoped latest-operation query with deterministic ordering and fail-closed Firestore errors. User/admin workload rows show only sanitized latest operation state/failure code, last healthy state, public binding state, and last activity time. Do not expose origins, service resources, exception text, key IDs, or internal audiences.

### Step 4.2: Keep edge telemetry truthful

Do not invent Access-seat or Worker counters from authorization events. The first production release may expose separately labeled central authorization successes/denials and Cloud Run request health; exact Cloudflare Access/Worker counters remain blocked until an authenticated, sanitized telemetry ingestion path and its IAM/cost contract are implemented and reviewed.

### Step 4.3: Verify views

```bash
cd plugins/madup-infra-manager/control-plane
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_dashboard_views \
  tests.integration.test_firestore_operations
```

Expected: PASS with safe owner/admin projections.

## Task 5: Finish exact Cloudflare plan/apply and canaries

**Files:**

- Modify: `plugins/madup-infra-manager/infra/edge/plan.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/apply.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/cloudflare_api.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/test_plan.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/test_apply.sh`
- Modify: `plugins/madup-infra-manager/infra/release/smoke_test.sh`
- Modify: `plugins/madup-infra-manager/infra/release/verify.sh`

### Step 5.1: Lock exact zone and route inventory

Require active `madup.app`, exact expected account, exact authoritative nameserver set, exact control/wildcard routes, no overlapping MIM route, exact three Access apps/policies, exactly two Worker secrets by name, and the reviewed Access seat limit.

### Step 5.2: Pin every mutation

Each action records the exact discovered resource ID or explicit absence sentinel plus a before-state hash. Apply compares state immediately before mutation, uses that exact ID, reads the exact resource immediately afterward, and blocks drift. Exact ready state yields zero actions.

### Step 5.3: Strengthen live denial canaries

Direct app-gateway `run.app` without proof must match the gateway's explicit missing-proof denial contract. Direct private user `run.app` must match Cloud Run IAM denial. A generic arbitrary `404` is not sufficient evidence.

### Step 5.4: Verify edge contracts

```bash
bash plugins/madup-infra-manager/infra/edge/test_plan.sh
bash plugins/madup-infra-manager/infra/edge/test_apply.sh
python3 -m unittest tests/test_mim_release_contract.py
```

Expected: PASS without a network or production mutation.

## Task 6: Independent review and full release proof

### Step 6.1: Run focused spec and security reviews

Review source persistence, GitHub refetch, exact Secret Manager access, breakglass separation, raw-header rejection, scheduler-only execution, Cloudflare immutable actions, and live canary specificity. Fix every critical/high and authorization/data/cost-affecting medium finding, then re-review.

### Step 6.2: Run the complete local matrix

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --local
```

Expected: exit 0 with plugin validation, Python lint/type/unit/integration, shell suites, Worker tests, Go test/vet/race/build, all three Docker builds, and generic secret scan.

### Step 6.3: Run the private exact-value release guard

Use the ignored exact denylist through the release verifier without reading or printing its contents:

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --release origin/main
```

Expected: worktree, index, outbound diff/blob/history, and exact-value scans pass; no protected/private file is tracked or staged.

### Step 6.4: Continue the existing exact production rollout

Only after Steps 6.1-6.3 pass, generate read-only GCP/Cloudflare plans, review exact targets/hashes, apply exact MIM resources, run live OAuth/MCP/app/IAM/cost/offboarding canaries, then create Lore commits and integrate/push `main`.
