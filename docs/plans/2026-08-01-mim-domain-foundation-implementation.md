# MIM Domain Foundation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Connect the bootstrap service to Cloud Run's default `run.app` URL with direct IAP inside an isolated MIM GCP boundary while leaving `madupai.com` unconfigured and reserving `mim.madupai.com` for the later employee-facing Cloudflare Access Managed OAuth path.

**Architecture:** A minimal dependency-free HTTP service provides a replaceable bootstrap target. Cloud Run direct IAP restricts access to an approved Madup identity on the service itself, with request-based billing, `min=0`, and `max=1`. This initial phase does not provision an external load balancer, DNS record, or nameserver change. Idempotent shell scripts pass `--project` to every GCP mutation and refuse to run unless the active account, project, organization, and domain exactly match a reviewed configuration.

**Tech Stack:** Claude plugin marketplace repository, Python 3 standard library, Docker, Bash, gcloud CLI, Cloud Run, request-based billing, direct IAP, Gabia DNS for the later deferred custom-domain path only.

---

## Preconditions and stop conditions

- Work in a dedicated git worktree created from the current `main` commit.
- Reauthenticate the reviewed operator account named by `MIM_OPERATOR_EMAIL` without creating Application Default Credentials.
- Select or create a GCP project dedicated to MIM. Never use any pre-existing sensitive project outside the reviewed MIM boundary.
- Confirm the selected project belongs to the Madup organization and has a reviewed Cloud Billing account.
- Do not change Gabia nameservers or the apex `madupai.com` record.
- Do not create or mutate any DNS record for `mim.madupai.com` in this phase.
- Do not provision an external load balancer, managed certificate, or domain mapping in this phase.
- Do not ask for, receive, or store Gabia passwords, Google passwords, MFA codes, service-account JSON keys, or OAuth refresh tokens.
- Do not ask employees to enter GCP project, organization, billing, Cloudflare, or operator values, and do not issue a shared API key. Those stay in operator-only configuration.
- Stop before billable provisioning if no isolated project and billing account can be proven.

### Task 1: Create an isolated implementation worktree

**Files:**
- No repository files changed.

**Step 1: Check the source tree is clean**

Run:

```bash
git -C /Users/madup/Documents/git/madup/claude-plugins status --short
```

Expected: no output.

**Step 2: Create the worktree**

Run:

```bash
git -C /Users/madup/Documents/git/madup/claude-plugins worktree add /Users/madup/Documents/git/madup/.worktrees/mim-domain -b feature/mim-domain
```

Expected: a new worktree checked out at `feature/mim-domain`.

**Step 3: Verify the worktree**

Run:

```bash
git -C /Users/madup/Documents/git/madup/.worktrees/mim-domain status --short --branch
```

Expected: `## feature/mim-domain` and no file changes.

### Task 2: Lock the bootstrap HTTP contract with tests

**Files:**
- Create: `plugins/madup-infra-manager/control-plane/bootstrap/app.py`
- Create: `plugins/madup-infra-manager/control-plane/bootstrap/test_app.py`
- Create: `plugins/madup-infra-manager/control-plane/bootstrap/Dockerfile`

**Step 1: Write the failing HTTP contract test**

Test the request handler directly with `unittest` and an in-memory response stream. Require:

- `GET /healthz` returns `200`, `Content-Type: application/json`, and `{"status":"ok"}`.
- `GET /` returns `200`, a UTF-8 HTML page containing `Madup Infra Manager`, and no environment variables or request headers.
- Unknown paths return `404`.

**Step 2: Run the test and verify it fails**

Run:

```bash
python3 -m unittest plugins/madup-infra-manager/control-plane/bootstrap/test_app.py -v
```

Expected: FAIL because `app.py` does not exist.

**Step 3: Implement the minimal server**

Use only `http.server`, `json`, and `os.environ.get("PORT", "8080")`. Bind to `0.0.0.0`. Return a static setup-in-progress page and never echo caller input.

**Step 4: Add a minimal container**

Use `python:3.13-slim`, copy only `app.py`, run as a non-root user, expose `8080`, and start with `python /app/app.py`.

**Step 5: Run tests and build locally**

Run:

```bash
python3 -m unittest plugins/madup-infra-manager/control-plane/bootstrap/test_app.py -v
docker build -t mim-bootstrap:test plugins/madup-infra-manager/control-plane/bootstrap
```

Expected: tests PASS and Docker build succeeds.

**Step 6: Commit**

Commit with a Lore message whose intent is to provide a safe, replaceable domain target. Record unit and container-build verification.

### Task 3: Add a fail-closed provisioning configuration

**Files:**
- Create: `plugins/madup-infra-manager/infra/domain/config.example.env`
- Create: `plugins/madup-infra-manager/infra/domain/preflight.sh`
- Create: `plugins/madup-infra-manager/infra/domain/test_preflight.sh`

**Step 1: Write failing preflight tests**

Stub `gcloud` through a temporary `PATH`. Verify the script rejects:

- Any account other than the reviewed operator account in `MIM_OPERATOR_EMAIL`.
- A missing or mismatched `MIM_PROJECT_ID`.
- Any project listed in the operator-only protected-project denylist.
- A project whose parent organization does not match `MIM_ORGANIZATION_ID`.
- A missing billing link.
- Any hostname other than exactly `mim.madupai.com`.
- A non-empty `MIM_APEX_ACTION` value other than `leave-unconfigured`.

Verify it accepts only a complete matching fixture and prints no access tokens or credentials.

**Step 2: Run the tests and verify they fail**

Run:

```bash
bash plugins/madup-infra-manager/infra/domain/test_preflight.sh
```

Expected: FAIL because `preflight.sh` does not exist.

**Step 3: Implement the preflight**

Require only the four public operator placeholders in the tracked example:

```bash
MIM_OPERATOR_EMAIL=<reviewed-operator-email>
MIM_PROJECT_ID=<reviewed-mim-project-id>
MIM_ORGANIZATION_ID=<reviewed-mim-organization-id>
MIM_BILLING_ACCOUNT_ID=<reviewed-mim-billing-account-id>
```

Stable region, hostname, apex handling, and initial IAP member are derived in code, not supplied by employees or tracked as public operator inputs. Every `gcloud` call must include both `--account="$MIM_OPERATOR_EMAIL"` and `--project="$MIM_PROJECT_ID"` where supported. Do not set the global gcloud account or project.

**Step 4: Run the tests**

Run:

```bash
bash plugins/madup-infra-manager/infra/domain/test_preflight.sh
```

Expected: all fail-closed cases PASS.

**Step 5: Commit**

Commit with a Lore message recording the forbidden projects and the explicit-account constraint.

### Task 4: Provision the private Cloud Run bootstrap service

**Files:**
- Create: `plugins/madup-infra-manager/infra/domain/apply_cloud_run.sh`
- Create: `plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh`

**Step 1: Write failing command-rendering tests**

Stub `gcloud` and verify the script renders only commands scoped to the configured project. Require:

- APIs: `run.googleapis.com`, `iap.googleapis.com`, `artifactregistry.googleapis.com`, `iam.googleapis.com`, and `cloudresourcemanager.googleapis.com`. The bootstrap image is built locally, so this phase does not enable Cloud Build or Compute Engine APIs.
- A dedicated Artifact Registry repository.
- Image build tagged with the current git commit SHA.
- Cloud Run request-based billing, service and revision limits both capped at one instance, `min=0`, 1 vCPU, 512 MiB, startup CPU boost disabled, no unauthenticated access, direct IAP enabled on the service, and ingress set to `all` so the default `run.app` URL remains the access path.
- The IAP service agent receives only `roles/run.invoker` on the service.
- Only `MIM_INITIAL_IAP_MEMBER` receives `roles/iap.httpsResourceAccessor`.

**Step 2: Run the tests and verify they fail**

Run:

```bash
bash plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh
```

Expected: FAIL because the apply script does not exist.

**Step 3: Implement idempotent provisioning**

Call `preflight.sh` first. Describe each resource before creating it. Never delete or replace an unrelated resource. If a same-named resource exists with a different project, region, or service target, stop with an actionable error.

Use Cloud Run direct IAP on the service itself. Do not create load-balancer resources, backend IAP, URL maps, forwarding rules, managed certificates, or domain mappings in this phase.

**Step 4: Run tests**

Run:

```bash
bash plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh
```

Expected: PASS.

**Step 5: Apply to the reviewed isolated project**

Run:

```bash
bash plugins/madup-infra-manager/infra/domain/preflight.sh
bash plugins/madup-infra-manager/infra/domain/apply_cloud_run.sh
```

Expected: the service exists with `Iap Enabled: true`, request-based billing, `min=0`, `max=1`, `ingress=all`, and no public invoker.

**Step 6: Verify default endpoint protection**

Run `gcloud run services describe` with explicit account, project, and region. Then request the default `run.app` URL without credentials.

Expected: IAP login or denial; never the bootstrap page.

**Step 7: Commit**

Commit with a Lore message recording exact deployed limits and IAP verification.

### Deferred: shared custom-domain ingress and DNS

**Files:** None. Deferred.

The initial launch intentionally stops at the direct `run.app` URL protected by direct IAP. The older shared-load-balancer and Gabia DNS tasks are superseded by that decision and must not be executed during this phase.

This bootstrap document does not announce the employee login flow as live. Production employee access remains POC-gated: browser Cloudflare Access Managed OAuth must prove authorization server metadata discovery, RFC 8707 `resource parameter`, PKCE S256, dynamic client registration or manual client ID fallback, callback compatibility, token refresh and reuse, and group-removal/session expiry latency before `mim.madupai.com` becomes the documented login path.

If Madup later makes an explicit cost and branding decision to add a shared custom domain, restart the design from this point and re-evaluate:

- Whether `mim.madupai.com` should become the primary custom hostname or remain reserved only.
- Whether `*.apps.madupai.com` should return as the long-term shared ingress pattern.
- Whether the new ingress path justifies an external load balancer, managed certificate, DNS changes, and any associated operating cost.

### Task 8: Final review and handoff

**Files:**
- Modify: `docs/plans/2026-08-01-madup-infra-manager-design.md` only if live constraints differ from the approved design.

**Step 1: Run repository checks**

Run:

```bash
git diff --check main...HEAD
python3 -m unittest plugins/madup-infra-manager/control-plane/bootstrap/test_app.py -v
bash plugins/madup-infra-manager/infra/domain/test_preflight.sh
bash plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh
```

Expected: all checks PASS.

**Step 2: Review the diff for secrets**

Run:

```bash
git diff --check main...HEAD
rg -n --hidden '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[0-9A-Za-z]{20,}|"private_key"\s*:)' --glob '!*.md' .
```

Expected: no secret matches.

**Step 3: Commit documentation changes**

Use a Lore commit. Record direct `run.app` access, direct IAP, request-based billing, `min=0`, `max=1`, and the deferred custom-domain decision, plus any known gaps.

**Step 4: Do not push automatically**

Leave the reviewed branch and external-state evidence ready for an explicit integration decision. The next plan covers the full dashboard, GitHub App admission, quotas, schedules, cost enforcement, and offboarding automation.
