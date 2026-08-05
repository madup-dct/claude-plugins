# MIM Public Release Sanitization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Publish MIM without exposing tenant-specific infrastructure metadata while preserving a zero-cloud-input employee experience and blocking contaminated outbound Git history before its first public push.

**Architecture:** Public code contains stable product policy, synthetic fixtures, and the public MIM hostname only. Exact GCP, Cloudflare, GitHub App, operator-mapping, and protected-project values remain in ignored operator files or managed secret stores. One Python standard-library scanner is shared by a versioned pre-push hook and manual release verifier; it scans local changes and every outbound commit, including deleted content and binary blobs.

**Tech Stack:** Python 3 standard library, Bash, Git plumbing, `unittest`, existing Claude plugin validation tools.

**Execution constraints:** Work may continue on local `main` because the user explicitly approved it, but no remote push is authorized. Before any public push, preserve the current local history under a local-only backup ref and reconstruct a clean candidate from the latest `origin/main`; never push the contaminated backup ref.

---

### Task 1: Lock the public/private configuration contract

**Files:**
- Create: `tests/test_mim_public_boundary.py`
- Modify: `plugins/madup-infra-manager/infra/domain/config.example.env`
- Modify: `plugins/madup-infra-manager/infra/domain/config_lib.sh`
- Modify: `plugins/madup-infra-manager/infra/domain/preflight.sh`
- Modify: `plugins/madup-infra-manager/infra/domain/apply_cloud_run.sh`
- Modify: `plugins/madup-infra-manager/infra/domain/test_preflight.sh`
- Modify: `plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh`
- Modify: `plugins/madup-infra-manager/infra/domain/.gitignore`

**Step 1: Write failing boundary tests**

Require the tracked example to contain placeholders rather than usable tenant values. Require bootstrap scripts to accept the exact environment boundary only from ignored operator configuration, derive fixed public policy such as region and hostname in code, and never contain a literal approved or protected project, organization, billing account, operator mapping, project number, or `run.app` origin.

The tests must also prove:

- no end-user path supplies cloud configuration;
- the operator email, project, organization, and billing account remain required operator-only values;
- active `gcloud` account, project parent, and linked billing still have to match that private configuration;
- legacy keys and unknown keys fail closed;
- errors name the failed key/check without echoing configured values;
- tests use synthetic fixtures only;
- `config.env` and any operator-only protected-project/denylist files are ignored.

**Step 2: Run and observe failure**

Run:

```bash
python3 -m unittest tests/test_mim_public_boundary.py -v
bash plugins/madup-infra-manager/infra/domain/test_preflight.sh
bash plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh
```

Expected: FAIL because tracked bootstrap files currently contain environment-specific values and the legacy operator key surface.

**Step 3: Implement the minimum central-operator boundary**

Rename the shell boundary consistently to the control-plane vocabulary, such as `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`. Keep stable public policy (`asia-northeast3`, `mim.madupai.com`, apex behavior) fixed in code. Derive the break-glass IAP member from the validated operator identity instead of accepting a second user-supplied copy.

The public example documents placeholders only. `config.env` remains local and ignored. Do not add real values to a test, comment, fixture, scanner rule, commit message, or documentation. Preserve the existing fail-closed `gcloud` checks and explicit `--account`/`--project` arguments; remove literal protected-project names from public code and rely on IAM isolation plus an operator-only exact denylist for bootstrap protection.

**Step 4: Verify**

Run the three commands from Step 2 plus:

```bash
python3 -m unittest tests/test_madup_infra_manager_plugin.py -v
git diff --check
```

Expected: all pass and no supplied operator value appears in error output.

**Step 5: Commit**

Commit intent: `Keep tenant infrastructure metadata outside the public bootstrap boundary`

### Task 2: Make first-run OAuth and central ownership explicit

**Files:**
- Modify: `README.md`
- Modify: `plugins/madup-infra-manager/skills/madup-infra-manager/SKILL.md`
- Modify: `plugins/madup-infra-manager/skills/madup-infra-manager/references/examples.md`
- Modify: `docs/plans/2026-08-01-madup-infra-manager-design.md`
- Modify: `docs/plans/2026-08-01-mim-domain-foundation-implementation.md`
- Modify: `docs/plans/2026-08-02-mim-control-plane-design.md`
- Modify: `docs/plans/2026-08-02-mim-control-plane-implementation.md`
- Modify: `tests/test_madup_infra_manager_plugin.py`
- Modify: `tests/test_mim_public_boundary.py`

**Step 1: Write failing documentation-contract tests**

Require the public UX contract to state:

1. install the plugin and make the first MIM request;
2. Claude opens Cloudflare Access Managed OAuth in a browser;
3. the employee signs in with the Madup Google Workspace identity and must belong to the MIM access group;
4. Claude stores and refreshes the resulting MCP token;
5. the employee never enters a GCP project, organization, billing account, operator identity, shared API key, or cloud credential;
6. Slack OAuth is optional and requested only for a Slack integration, with least scopes and direct secret storage;
7. group removal denies new/renewed access and the periodic identity reconciler quarantines workloads and starts the transfer/cleanup lifecycle.

Also require every public plan to describe the cloud boundary generically rather than naming its tenant-specific instance. Stable public values such as `mim.madupai.com`, `madupmarketing`, `madup.com`, and the fixed product limits may remain.

**Step 2: Run and observe failure**

Run:

```bash
python3 -m unittest tests/test_madup_infra_manager_plugin.py tests/test_mim_public_boundary.py -v
```

Expected: FAIL because current plans still name the private deployment boundary and the plugin UX does not fully lock first-run OAuth semantics.

**Step 3: Update only the public contract**

Describe Cloudflare Access Managed OAuth as the primary MCP login, backed by Google Workspace policy. Document a mandatory staging proof of concept for OAuth discovery, RFC 8707 resource indicators, PKCE, dynamic client registration or preconfigured client fallback, browser callback behavior, token refresh, JWT audience/issuer validation, and group-removal latency.

Keep Slack installation separate from MIM identity. Never ask the user to paste Slack tokens into Claude; an authenticated OAuth callback stores them directly in Secret Manager and only secret metadata is shown later.

Move real operator values to the private deployment configuration conceptually; do not create a tracked private runbook in this public repository.

**Step 4: Verify**

Run:

```bash
python3 -m unittest tests/test_madup_infra_manager_plugin.py tests/test_mim_public_boundary.py -v
claude plugin validate --strict .
git diff --check
```

Expected: all pass.

**Step 5: Commit**

Commit intent: `Make employee login simple without publishing operator configuration`

### Task 3: Implement the outbound-history scanner

**Files:**
- Create: `plugins/madup-infra-manager/infra/release/public_release_guard.py`
- Create: `plugins/madup-infra-manager/infra/release/.gitignore`
- Create: `tests/test_public_release_guard.py`

**Step 1: Write failing scanner tests in temporary Git repositories**

Use `tempfile.TemporaryDirectory` and local Git repositories. Construct secret-shaped fixtures at runtime from separate string fragments so the test source does not itself contain a match. Cover:

- exact local-denylist match in a tracked worktree file;
- exact match only in the index;
- exact or generic match in an older outbound commit that is absent from the final tree;
- a match visible only on a deletion line in commit history;
- added, modified, renamed, copied, and deleted paths;
- a binary blob;
- a new branch whose remote SHA is all zeroes;
- a deletion push whose local SHA is all zeroes;
- multiple ref updates on pre-push stdin;
- missing or unreadable exact denylist in release/pre-push mode;
- Git plumbing failure;
- output that reports only rule, scope, abbreviated commit, and path, never the matched value;
- stable public MIM values and clearly synthetic fixtures remaining clean;
- a finding already present at the supplied public baseline not blocking solely because an unrelated line changed, while a new path or new finding fingerprint does block.

**Step 2: Run and observe failure**

Run:

```bash
python3 -m unittest tests/test_public_release_guard.py -v
```

Expected: FAIL because the scanner does not exist.

**Step 3: Implement one standard-library scanner**

Provide these interfaces:

```text
public_release_guard.py verify --local [--base-ref REF]
public_release_guard.py verify --range BASE..HEAD
public_release_guard.py pre-push REMOTE_NAME REMOTE_URL
```

Load exact values from `MIM_PUBLIC_RELEASE_DENYLIST_FILE` or the ignored default `plugins/madup-infra-manager/infra/release/denylist.exact`. Ignore blank lines and comments. Release and pre-push modes exit `3` if that file is missing or unreadable. Never include an exact production value in source or fixtures.

Use high-confidence contextual generic rules for private-key blocks, service-account JSON, GitHub/Google/Slack tokens, OAuth/client/refresh secrets, and generated `run.app` origins. Do not broadly reject plain numbers or ordinary email addresses.

For each pushed ref:

- skip deletion pushes;
- use `remote_sha..local_sha` for an existing remote ref;
- for a new ref, use commits reachable from the local SHA but not from that remote's refs, falling back fail-closed when no reliable base exists;
- inspect zero-context patches so introduced-then-deleted values remain visible;
- inspect each changed raw blob so binary content is not skipped;
- compare findings with the supplied remote baseline by path, rule, and value fingerprint so already-public unchanged findings may warn without allowing propagation to a new path;
- never print matched bytes or lines.

Exit codes: `0` clean, `2` usage/config, `3` denylist unavailable, `4` Git failure, `10` local/index violation, `11` outbound blob violation, `12` outbound diff violation.

**Step 4: Verify**

Run:

```bash
python3 -m unittest tests/test_public_release_guard.py -v
python3 -m compileall -q plugins/madup-infra-manager/infra/release/public_release_guard.py
git diff --check
```

Expected: all pass.

**Step 5: Commit**

Commit intent: `Block private metadata anywhere in outbound Git history`

### Task 4: Wire the guard into release and pre-push paths

**Files:**
- Create: `plugins/madup-infra-manager/infra/release/verify.sh`
- Create: `plugins/madup-infra-manager/infra/release/install_git_hooks.sh`
- Create: `.githooks/pre-push`
- Create: `tests/test_mim_release_contract.py`
- Modify: `README.md`

**Step 1: Write failing integration-contract tests**

Require:

- the hook to forward the remote name, remote URL, and untouched stdin to the scanner;
- the installer to set repository-local `core.hooksPath=.githooks` and verify executable bits;
- `verify.sh --ci` to run generic scanner and repository contracts but clearly state that CI cannot approve the first public push;
- `verify.sh --release BASE_REF` to require the exact denylist and scan both current changes and `BASE_REF..HEAD`;
- the ignored exact denylist path never to be tracked;
- wrappers to propagate scanner failures unchanged and never deploy, mutate cloud state, or echo secrets.

**Step 2: Run and observe failure**

Run:

```bash
python3 -m unittest tests/test_mim_release_contract.py -v
```

Expected: FAIL because the hook and wrappers do not exist.

**Step 3: Implement thin wrappers**

Keep all detection logic in `public_release_guard.py`. Shell files only locate the repository, validate arguments, and delegate. Document installation and both verifier modes. Do not install a bypass flag.

**Step 4: Verify and install locally**

Run:

```bash
python3 -m unittest tests/test_public_release_guard.py tests/test_mim_release_contract.py -v
bash plugins/madup-infra-manager/infra/release/install_git_hooks.sh
git config --get core.hooksPath
git diff --check
```

Expected: tests pass and the configured hook path is `.githooks`. A pre-push attempt without the ignored exact denylist must fail closed before network transfer.

**Step 5: Commit**

Commit intent: `Make public pushes prove their complete outbound history is clean`

### Task 5: Sanitize the current tree and create the local release denylist

**Files:**
- Modify: every tracked file identified by `tests/test_mim_public_boundary.py` or the exact-value scan
- Create locally but never track: `plugins/madup-infra-manager/infra/release/denylist.exact`

**Step 1: Run the generic and exact scans and observe the expected block**

Populate the ignored denylist from the private operator inventory without copying values into a command transcript, test, commit, or tracked file. Set mode `0600`. Include every tenant-specific GCP boundary value, generated origin, Cloudflare account/zone/team identifier, GitHub App identifier, and private operator mapping that must not become public.

Run:

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --release origin/main
```

Expected: FAIL because the unpublished branch history and possibly the current tree still contain exact values.

**Step 2: Remove all current-tree findings**

Replace public examples with placeholders and tests with synthetic values. Preserve public product names and policy where intentional. Do not weaken the denylist or add broad allowlists to make the scan pass.

**Step 3: Verify the current tree separately**

Run:

```bash
python3 plugins/madup-infra-manager/infra/release/public_release_guard.py verify --local --base-ref origin/main
python3 -m unittest discover -s tests -p 'test_*.py'
claude plugin validate --strict .
git diff --check
```

Expected: current tree is clean. The release range may still fail because old unpublished commits remain reachable; that failure is required evidence for Task 6.

**Step 4: Commit**

Commit intent: `Remove tenant identifiers from every publishable MIM artifact`

### Task 6: Rebuild unpublished history on the latest public main

**Files:**
- No additional product files expected; this task reconstructs Git history from already-sanitized final content.

**Step 1: Inventory and preserve**

Fetch the latest `origin/main`. Confirm the worktree is clean. Create a clearly named local-only backup branch/ref at the current HEAD and record its SHA in the private release notes. Never push that backup. Inventory every path and commit in `origin/main..backup`; classify any non-MIM local-only work so it is not silently dropped.

Do not use `git reset --hard` or delete refs.

**Step 2: Create an isolated clean candidate**

Create a candidate branch/worktree from the latest `origin/main`. Export only the sanitized final-state diffs for MIM-owned paths and deliberately reviewed shared files. Apply those diffs onto the candidate; resolve shared README/marketplace changes against the latest public version rather than replacing it wholesale.

Old unpublished commits must not be cherry-picked, merged, or made parents of the candidate, because their blobs contain the values being removed.

**Step 3: Commit fresh Lore history**

Create the smallest reviewable set of fresh commits whose trees contain only sanitized artifacts. Preserve institutional reasoning in docs and Lore trailers; do not preserve contaminated blobs merely for commit granularity.

**Step 4: Prove the candidate is clean**

Run:

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --release origin/main
python3 -m unittest discover -s tests -p 'test_*.py'
uv run --project plugins/madup-infra-manager/control-plane python -m unittest discover -s plugins/madup-infra-manager/control-plane/tests -p 'test_*.py'
uv run --project plugins/madup-infra-manager/control-plane ruff check plugins/madup-infra-manager/control-plane
uv run --project plugins/madup-infra-manager/control-plane mypy plugins/madup-infra-manager/control-plane/src
claude plugin validate --strict .
git diff --check
```

Expected: all pass, and the scanner finds no exact or generic violation in any commit reachable from `origin/main..candidate`.

**Step 5: Replace local main safely, without pushing**

After verification, move local `main` to the clean candidate while retaining the local-only backup ref. Re-run the release verifier on local `main`. Report that remote publication remains pending explicit push authorization.

### Task 7: Independent release review

**Files:**
- Review only.

**Step 1: Spec review**

An independent reviewer verifies the zero-cloud-input user flow, operator-only configuration, public/private metadata split, exact-denylist behavior, first-push history coverage, and preservation of unrelated public work.

**Step 2: Security and code-quality review**

An independent security reviewer attempts bypasses involving missing denylist, new branches, multiple refs, rename/copy, deletion-only history, binaries, malformed Git objects, synthetic-fixture evasion, output leakage, hook bypass documentation, and baseline suppression.

**Step 3: Final verification**

Repeat Task 6 Step 4 from the clean local `main`. Do not claim the repository is safe to publish until both reviewers approve and all commands pass.
