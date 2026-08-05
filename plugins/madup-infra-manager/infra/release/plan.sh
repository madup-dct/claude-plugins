#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/task18_lib.sh"
CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"
TENANT_EVIDENCE_FILE="${MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE:-$(mim_task18_default_tenant_evidence_file "$SCRIPT_DIR")}"

MODE=
PLAN_OUT=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --plan) MODE=plan; shift ;;
    --out) PLAN_OUT=$2; shift 2 ;;
    --*) mim_task18_fail "Unknown argument: $1" ;;
    *) mim_task18_fail "Positional arguments are not supported" ;;
  esac
done
[[ "$MODE" == plan && -n "$PLAN_OUT" ]] || mim_task18_fail "Usage: plan.sh --plan --out .state/<name>.json"
mim_task18_assert_plan_create_path "$SCRIPT_DIR" "$PLAN_OUT"

TMP_DIR=$(mktemp -d)
SNAPSHOT_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR" "$SNAPSHOT_DIR"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v git >/dev/null 2>&1 || mim_task18_fail "git is required"
command -v gcloud >/dev/null 2>&1 || mim_task18_fail "gcloud CLI is required"
SLACK_ENABLED="$MIM_SLACK_ENABLED"
if [[ "$SLACK_ENABLED" == "true" ]]; then
  command -v curl >/dev/null 2>&1 || mim_task18_fail "curl is required"
fi

SOURCE_COMMIT=$(git -C "$(mim_task18_repo_root "$SCRIPT_DIR")" rev-parse HEAD)
GENERATED_AT="${MIM_TASK18_PLAN_GENERATED_AT:-$(mim_task18_now_epoch)}"
EXPIRES_AT="${MIM_TASK18_PLAN_EXPIRES_AT:-$((GENERATED_AT + MIM_TASK18_PLAN_MAX_AGE_SECONDS))}"
CONFIG_FINGERPRINT=$(mim_task18_config_fingerprint "$SNAPSHOT_CONFIG")

ACTIVE_ACCOUNT=$(mim_task18_assert_active_gcloud_account)

PROJECT_ID_CHECK=$(mim_task18_gcloud_capture \
  "Unable to describe the configured project" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(projectId)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_ID_CHECK" == "$MIM_PROJECT_ID" ]] || mim_task18_fail "Configured project mismatch"

PROJECT_PARENT_TYPE=$(mim_task18_gcloud_capture \
  "Unable to determine the project parent type" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(parent.type)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_PARENT_TYPE" == "organization" ]] || mim_task18_fail "Project parent must be an organization"

PROJECT_PARENT_ID=$(mim_task18_gcloud_capture \
  "Unable to determine the project organization" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(parent.id)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_PARENT_ID" == "$MIM_ORGANIZATION_ID" ]] || mim_task18_fail "Project organization mismatch"

PROJECT_NUMBER=$(mim_task18_gcloud_capture \
  "Unable to determine the project number" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(projectNumber)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || mim_task18_fail "Invalid project number returned"

BILLING_ENABLED=$(mim_task18_gcloud_capture \
  "Unable to determine project billing status" \
  billing projects describe "$MIM_PROJECT_ID" \
  '--format=value(billingEnabled)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$BILLING_ENABLED" == "True" ]] || mim_task18_fail "Billing must be linked"

BILLING_ACCOUNT_NAME=$(mim_task18_gcloud_capture \
  "Unable to determine the billing account" \
  billing projects describe "$MIM_PROJECT_ID" \
  '--format=value(billingAccountName)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$BILLING_ACCOUNT_NAME" == "billingAccounts/$MIM_BILLING_ACCOUNT_ID" ]] || mim_task18_fail "Billing account mismatch"

RELEASE_IDENTITY_EMAIL=$(mim_task18_release_identity_email)
RELEASE_IDENTITY_STATE_FILE="$TMP_DIR/release-identity.txt"
RELEASE_IDENTITY_STATE=$(mim_task18_gcloud_optional_output \
  "Unable to inspect the release identity" \
  "$RELEASE_IDENTITY_STATE_FILE" \
  iam service-accounts describe "$RELEASE_IDENTITY_EMAIL" \
  '--format=value(email)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")

BLOCKERS_FILE="$TMP_DIR/blockers.tsv"
: >"$BLOCKERS_FILE"
append_blocker() {
  printf '%s\t%s\n' "$1" "$2" >>"$BLOCKERS_FILE"
}

SLACK_CONFIG_TOKEN=${MIM_TASK18_SLACK_CONFIG_TOKEN-}
SLACK_MANIFEST_JSON="$TMP_DIR/slack-manifest.json"
slack_redirect_uri=
slack_org_deploy_enabled=
slack_bot_scopes_csv=
slack_user_scopes_csv=
if [[ "$SLACK_ENABLED" == "true" ]]; then
  if [[ -z "$SLACK_CONFIG_TOKEN" ]]; then
    append_blocker "missing-slack-config-token" "Slack app configuration access token is required for live manifest export validation."
  else
  slack_request_body="$TMP_DIR/slack-request.json"
  APP_ID="$MIM_SLACK_APP_ID" CONFIG_TOKEN="$SLACK_CONFIG_TOKEN" python3 - "$slack_request_body" <<'PY'
import json, os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({"app_id": os.environ["APP_ID"], "token": os.environ["CONFIG_TOKEN"]}, sort_keys=True) + "\n")
PY
  if curl -sS -X POST 'https://slack.com/api/apps.manifest.export' -H 'Content-Type: application/json' --data-binary @"$slack_request_body" >"$SLACK_MANIFEST_JSON"; then
    IFS=$'\t' read -r slack_redirect_uri slack_org_deploy_enabled slack_bot_scopes_csv slack_user_scopes_csv <<<"$(python3 - "$SLACK_MANIFEST_JSON" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
if payload.get("ok") is not True:
    raise SystemExit(1)
manifest = payload.get("manifest")
oauth_config = manifest.get("oauth_config") if isinstance(manifest, dict) else None
settings = manifest.get("settings") if isinstance(manifest, dict) else None
redirect_urls = oauth_config.get("redirect_urls") if isinstance(oauth_config, dict) else None
scopes = oauth_config.get("scopes") if isinstance(oauth_config, dict) else None
bot = scopes.get("bot") if isinstance(scopes, dict) else None
user = scopes.get("user", []) if isinstance(scopes, dict) else []
org = settings.get("org_deploy_enabled") if isinstance(settings, dict) else None
if not (isinstance(redirect_urls, list) and len(redirect_urls) == 1 and isinstance(redirect_urls[0], str)):
    raise SystemExit(1)
if not (isinstance(bot, list) and all(isinstance(item, str) for item in bot)):
    raise SystemExit(1)
if not (isinstance(user, list) and all(isinstance(item, str) for item in user)):
    raise SystemExit(1)
if not isinstance(org, bool):
    raise SystemExit(1)
print("\t".join([redirect_urls[0], "true" if org else "false", ",".join(sorted(bot)), ",".join(sorted(user))]))
PY
)"
    expected_org_deploy_enabled=false
    [[ "$MIM_SLACK_APPROVED_ORG_ID" != "none" ]] && expected_org_deploy_enabled=true
    [[ "$slack_redirect_uri" == "$MIM_TASK18_SLACK_REDIRECT_URI" ]] || append_blocker "slack-manifest-redirect-mismatch" "Slack manifest redirect URI does not match the reviewed callback."
    [[ "$slack_bot_scopes_csv" == "$MIM_TASK18_SLACK_REQUIRED_SCOPES" ]] || append_blocker "slack-manifest-bot-scopes-mismatch" "Slack manifest bot scopes do not match the reviewed least-privilege set."
    [[ -z "$slack_user_scopes_csv" ]] || append_blocker "slack-manifest-user-scopes-mismatch" "Slack manifest user scopes must remain empty for the reviewed install."
    [[ "$slack_org_deploy_enabled" == "$expected_org_deploy_enabled" ]] || append_blocker "slack-manifest-org-deploy-mismatch" "Slack manifest deploy mode does not match the reviewed organization install mode."
  else
    append_blocker "slack-manifest-export-failed" "Slack manifest export failed."
  fi
  fi
fi

tenant_evidence_hash=
tenant_evidence_app_id=
tenant_evidence_org_id=
tenant_evidence_workspace_ids_csv=
tenant_evidence_version=
if [[ "$SLACK_ENABLED" == "true" ]]; then
  if [[ ! -e "$TENANT_EVIDENCE_FILE" || ! -e "$TENANT_EVIDENCE_FILE.sha256" ]]; then
    append_blocker "missing-slack-tenant-evidence" "Reviewed Slack tenant evidence artifact is required."
  else
  mim_task18_assert_state_json_read_path "$SCRIPT_DIR" "$TENANT_EVIDENCE_FILE"
  expected_hash=$(awk '{print $1}' "$TENANT_EVIDENCE_FILE.sha256")
  actual_hash=$(mim_task18_sha256_file "$TENANT_EVIDENCE_FILE")
  if [[ "$expected_hash" != "$actual_hash" ]]; then
    append_blocker "slack-tenant-evidence-hash-mismatch" "Slack tenant evidence hash does not match its SHA sidecar."
  else
    IFS=$'\t' read -r tenant_evidence_version tenant_evidence_generated_at tenant_evidence_app_id tenant_evidence_org_id tenant_evidence_workspace_ids_csv <<<"$(python3 - "$TENANT_EVIDENCE_FILE" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
workspace_ids = payload.get("approved_workspace_ids")
if not isinstance(workspace_ids, list) or any(not isinstance(item, str) for item in workspace_ids):
    raise SystemExit(1)
print("\t".join([
    str(payload.get("version", "")),
    str(payload.get("generated_at_epoch", "")),
    str(payload.get("app_id", "")),
    str(payload.get("approved_org_id", "")),
    ",".join(sorted(workspace_ids)),
]))
PY
)"
    [[ "$tenant_evidence_version" == "$MIM_TASK18_SLACK_TENANT_EVIDENCE_VERSION" ]] || append_blocker "slack-tenant-evidence-schema-invalid" "Slack tenant evidence schema version is invalid."
    [[ "$tenant_evidence_generated_at" =~ ^[0-9]+$ ]] || append_blocker "slack-tenant-evidence-schema-invalid" "Slack tenant evidence generated_at is invalid."
    if [[ "$tenant_evidence_generated_at" =~ ^[0-9]+$ ]]; then
      (( tenant_evidence_generated_at <= GENERATED_AT )) || append_blocker "slack-tenant-evidence-schema-invalid" "Slack tenant evidence generated_at is invalid."
      (( GENERATED_AT - tenant_evidence_generated_at <= MIM_TASK18_PLAN_MAX_AGE_SECONDS )) || append_blocker "stale-slack-tenant-evidence" "Slack tenant evidence must be no older than 30 minutes."
    fi
    [[ "$tenant_evidence_app_id" == "$MIM_SLACK_APP_ID" ]] || append_blocker "slack-tenant-evidence-app-id-mismatch" "Slack tenant evidence app ID does not match the reviewed app."
    [[ "$tenant_evidence_org_id" == "$MIM_SLACK_APPROVED_ORG_ID" ]] || append_blocker "slack-tenant-evidence-org-mismatch" "Slack tenant evidence organization ID does not match the reviewed organization."
    expected_workspace_ids_csv=$(printf '%s\n' ${MIM_SLACK_APPROVED_WORKSPACE_IDS//,/ } | sort | paste -sd ',' -)
    [[ "$tenant_evidence_workspace_ids_csv" == "$expected_workspace_ids_csv" ]] || append_blocker "slack-tenant-evidence-workspace-mismatch" "Slack tenant evidence workspace IDs do not match the reviewed allowlist."
    tenant_evidence_hash=$actual_hash
  fi
  fi
fi

app_access_issuer=${MIM_TASK18_APP_CLOUDFLARE_ACCESS_ISSUER-}
app_access_audience=${MIM_TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE-}
app_gateway_current_key_id=${MIM_TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID-}
app_gateway_previous_key_id=${MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID-}
app_gateway_previous_secret_version=${MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION-}

mim_task18_validate_https_origin "$app_access_issuer" || append_blocker "missing-app-access-issuer" "Reviewed app Access issuer must be an exact HTTPS Cloudflare Access origin."
mim_task18_validate_cloudflare_audience "$app_access_audience" || append_blocker "missing-app-access-audience" "Reviewed app Access audience must be a safe non-empty audience string."
mim_task18_validate_key_id "$app_gateway_current_key_id" || append_blocker "missing-app-gateway-current-key-id" "Reviewed app-gateway current proof key ID is required."
if [[ -n "$app_gateway_previous_key_id" || -n "$app_gateway_previous_secret_version" ]]; then
  mim_task18_validate_key_id "$app_gateway_previous_key_id" || append_blocker "invalid-app-gateway-previous-key-id" "Reviewed app-gateway previous proof key ID must be a safe non-empty identifier when provided."
  mim_task18_validate_secret_version_ref "$app_gateway_previous_secret_version" "$(mim_task18_secret_name_from_ref "$app_gateway_previous_secret_version")" || append_blocker "invalid-app-gateway-previous-secret-version" "Reviewed app-gateway previous proof secret version must be a fully pinned numeric Secret Manager version."
fi
if [[ -n "$app_gateway_previous_key_id" && "$app_gateway_previous_key_id" == "$app_gateway_current_key_id" ]]; then
  append_blocker "duplicate-app-gateway-proof-key-id" "Reviewed app-gateway previous proof key ID must differ from the current key ID."
fi

builder_image_uri=${MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI-}
app_gateway_image_uri=${MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI-}
runtime_image_uri=${MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI-}
builder_build_id=${MIM_TASK18_REVIEWED_BUILDER_BUILD_ID-}
app_gateway_build_id=${MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID-}
runtime_build_id=${MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID-}
bootstrap_secret_version=${MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION-}
app_gateway_proof_secret_version=${MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION-}

mim_task18_validate_digest_image_uri "$builder_image_uri" "mim-platform" || append_blocker "missing-reviewed-builder-image" "Reviewed builder image URI must be a digest-pinned central Artifact Registry image."
mim_task18_validate_digest_image_uri "$app_gateway_image_uri" "mim-platform" || append_blocker "missing-reviewed-app-gateway-image" "Reviewed app-gateway image URI must be a digest-pinned central Artifact Registry image."
mim_task18_validate_digest_image_uri "$runtime_image_uri" "mim-control-plane" || append_blocker "missing-reviewed-runtime-image" "Reviewed runtime image URI must be a digest-pinned central Artifact Registry image."
[[ "$builder_build_id" =~ ^[A-Za-z0-9-]{6,128}$ ]] || append_blocker "missing-reviewed-builder-build" "Reviewed builder Cloud Build ID is required."
[[ "$app_gateway_build_id" =~ ^[A-Za-z0-9-]{6,128}$ ]] || append_blocker "missing-reviewed-app-gateway-build" "Reviewed app-gateway Cloud Build ID is required."
[[ "$runtime_build_id" =~ ^[A-Za-z0-9-]{6,128}$ ]] || append_blocker "missing-reviewed-runtime-build" "Reviewed runtime Cloud Build ID is required."
mim_task18_validate_bootstrap_secret_version_ref "$bootstrap_secret_version" || append_blocker "missing-runtime-bootstrap-secret-version" "Reviewed runtime bootstrap secret version must be an exact numeric central Secret Manager version."
mim_task18_validate_secret_version_ref "$app_gateway_proof_secret_version" "$MIM_TASK18_APP_GATEWAY_PROOF_SECRET_NAME" || append_blocker "missing-app-gateway-proof-secret-version" "Reviewed app-gateway proof secret version must be an exact numeric central Secret Manager version."

builder_build_path="$TMP_DIR/builder-build.json"
app_gateway_build_path="$TMP_DIR/app-gateway-build.json"
runtime_build_path="$TMP_DIR/runtime-build.json"
builder_artifact_path="$TMP_DIR/builder-artifact.json"
app_gateway_artifact_path="$TMP_DIR/app-gateway-artifact.json"
runtime_artifact_path="$TMP_DIR/runtime-artifact.json"
bootstrap_secret_path="$TMP_DIR/bootstrap-secret.json"
app_gateway_proof_secret_path="$TMP_DIR/app-gateway-proof-secret.json"

builder_build_state=missing
app_gateway_build_state=missing
runtime_build_state=missing
builder_artifact_state=missing
app_gateway_artifact_state=missing
runtime_artifact_state=missing
bootstrap_secret_state=missing
app_gateway_proof_secret_state=missing

if [[ "$builder_build_id" =~ ^[A-Za-z0-9-]{6,128}$ ]]; then
  builder_build_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed builder Cloud Build" \
    "$builder_build_path" \
    builds describe "$builder_build_id" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$builder_build_state" == "exists" ]] || append_blocker "reviewed-builder-build-missing" "Reviewed builder Cloud Build ID does not exist."
fi

if [[ "$runtime_build_id" =~ ^[A-Za-z0-9-]{6,128}$ ]]; then
  runtime_build_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed runtime Cloud Build" \
    "$runtime_build_path" \
    builds describe "$runtime_build_id" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$runtime_build_state" == "exists" ]] || append_blocker "reviewed-runtime-build-missing" "Reviewed runtime Cloud Build ID does not exist."
fi

if [[ "$app_gateway_build_id" =~ ^[A-Za-z0-9-]{6,128}$ ]]; then
  app_gateway_build_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed app-gateway Cloud Build" \
    "$app_gateway_build_path" \
    builds describe "$app_gateway_build_id" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$app_gateway_build_state" == "exists" ]] || append_blocker "reviewed-app-gateway-build-missing" "Reviewed app-gateway Cloud Build ID does not exist."
fi

if mim_task18_validate_digest_image_uri "$builder_image_uri" "mim-platform"; then
  builder_artifact_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed builder Artifact Registry digest" \
    "$builder_artifact_path" \
    artifacts docker images describe "$builder_image_uri" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$builder_artifact_state" == "exists" ]] || append_blocker "reviewed-builder-image-missing" "Reviewed builder Artifact Registry digest does not exist."
fi

if mim_task18_validate_digest_image_uri "$runtime_image_uri" "mim-control-plane"; then
  runtime_artifact_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed runtime Artifact Registry digest" \
    "$runtime_artifact_path" \
    artifacts docker images describe "$runtime_image_uri" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$runtime_artifact_state" == "exists" ]] || append_blocker "reviewed-runtime-image-missing" "Reviewed runtime Artifact Registry digest does not exist."
fi

if mim_task18_validate_digest_image_uri "$app_gateway_image_uri" "mim-platform"; then
  app_gateway_artifact_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed app-gateway Artifact Registry digest" \
    "$app_gateway_artifact_path" \
    artifacts docker images describe "$app_gateway_image_uri" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$app_gateway_artifact_state" == "exists" ]] || append_blocker "reviewed-app-gateway-image-missing" "Reviewed app-gateway Artifact Registry digest does not exist."
fi

if mim_task18_validate_bootstrap_secret_version_ref "$bootstrap_secret_version"; then
  bootstrap_secret_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed runtime bootstrap secret version" \
    "$bootstrap_secret_path" \
    secrets versions describe "$(mim_task18_bootstrap_secret_version_number "$bootstrap_secret_version")" \
    "--secret=$MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_NAME" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$bootstrap_secret_state" == "exists" ]] || append_blocker "runtime-bootstrap-secret-version-missing" "Reviewed runtime bootstrap secret version does not exist."
fi

if mim_task18_validate_secret_version_ref "$app_gateway_proof_secret_version" "$MIM_TASK18_APP_GATEWAY_PROOF_SECRET_NAME"; then
  app_gateway_proof_secret_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect reviewed app-gateway proof secret version" \
    "$app_gateway_proof_secret_path" \
    secrets versions describe "$(mim_task18_bootstrap_secret_version_number "$app_gateway_proof_secret_version")" \
    "--secret=$MIM_TASK18_APP_GATEWAY_PROOF_SECRET_NAME" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$app_gateway_proof_secret_state" == "exists" ]] || append_blocker "app-gateway-proof-secret-version-missing" "Reviewed app-gateway proof secret version does not exist."
fi

MAINTENANCE_SERVICE_ACCOUNT="mim-maintenance@$MIM_PROJECT_ID.iam.gserviceaccount.com"
PROJECT_IAM_PATH="$TMP_DIR/project-iam.json"
RELEASE_IAM_PATH="$TMP_DIR/release-iam.json"
MAINTENANCE_IAM_PATH="$TMP_DIR/maintenance-iam.json"

gcloud projects get-iam-policy "$MIM_PROJECT_ID" \
  '--format=json' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID" >"$PROJECT_IAM_PATH"

if [[ "$RELEASE_IDENTITY_STATE" == "exists" ]]; then
  gcloud iam service-accounts get-iam-policy "$RELEASE_IDENTITY_EMAIL" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >"$RELEASE_IAM_PATH"
else
  printf '{}\n' >"$RELEASE_IAM_PATH"
  append_blocker "missing-release-identity" "Release identity metadata must exist before release apply can proceed."
fi

MAINTENANCE_IDENTITY_FILE="$TMP_DIR/maintenance-identity.txt"
MAINTENANCE_IDENTITY_STATE=$(mim_task18_gcloud_optional_output \
  "Unable to inspect the maintenance identity" \
  "$MAINTENANCE_IDENTITY_FILE" \
  iam service-accounts describe "$MAINTENANCE_SERVICE_ACCOUNT" \
  '--format=value(email)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
if [[ "$MAINTENANCE_IDENTITY_STATE" == "exists" ]]; then
  gcloud iam service-accounts get-iam-policy "$MAINTENANCE_SERVICE_ACCOUNT" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >"$MAINTENANCE_IAM_PATH"
else
  printf '{}\n' >"$MAINTENANCE_IAM_PATH"
  append_blocker "missing-maintenance-identity" "Maintenance identity metadata must exist before release apply can proceed."
fi

job_names=(mim-identity-sync mim-lifecycle mim-usage-ingest)
for job_name in "${job_names[@]}"; do
  slug=${job_name//-/_}
  describe_path="$TMP_DIR/${slug}-job.json"
  state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect Cloud Run job $job_name" \
    "$describe_path" \
    run jobs describe "$job_name" \
    --region="$MIM_TASK18_FIXED_REGION" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  printf -v "JOB_${slug}_STATE" '%s' "$state"
  printf -v "JOB_${slug}_PATH" '%s' "$describe_path"
done

for scheduler_name in "${job_names[@]}"; do
  slug=${scheduler_name//-/_}
  describe_path="$TMP_DIR/${slug}-scheduler.json"
  state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect Cloud Scheduler job $scheduler_name" \
    "$describe_path" \
    scheduler jobs describe "$scheduler_name" \
    --location="$MIM_TASK18_FIXED_REGION" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  printf -v "SCHEDULER_${slug}_STATE" '%s' "$state"
  printf -v "SCHEDULER_${slug}_PATH" '%s' "$describe_path"
done

service_names=(mim-control-plane mim-app-gateway mim-deploy-worker mim-schedule-gateway)
for service_name in "${service_names[@]}"; do
  slug=${service_name//-/_}
  describe_path="$TMP_DIR/${slug}-service.json"
  iam_path="$TMP_DIR/${slug}-iam.json"
  state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect Cloud Run service $service_name" \
    "$describe_path" \
    run services describe "$service_name" \
    --region="$MIM_TASK18_FIXED_REGION" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  printf -v "SERVICE_${slug}_STATE" '%s' "$state"
  printf -v "SERVICE_${slug}_DESCRIBE_PATH" '%s' "$describe_path"
  printf -v "SERVICE_${slug}_IAM_PATH" '%s' "$iam_path"
  if [[ "$state" == "exists" ]]; then
    gcloud run services get-iam-policy "$service_name" \
      --region="$MIM_TASK18_FIXED_REGION" \
      '--format=json' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID" >"$iam_path"
  else
    printf '{}\n' >"$iam_path"
  fi
done

PLAN_PATH="$TMP_DIR/release-plan.json"
PLAN_GENERATED_AT="$GENERATED_AT" \
PLAN_EXPIRES_AT="$EXPIRES_AT" \
PLAN_CONFIG_FINGERPRINT="$CONFIG_FINGERPRINT" \
PLAN_OPERATOR_EMAIL="$MIM_OPERATOR_EMAIL" \
PLAN_PROJECT_ID="$MIM_PROJECT_ID" \
PLAN_ORGANIZATION_ID="$MIM_ORGANIZATION_ID" \
PLAN_BILLING_ACCOUNT_ID="$MIM_BILLING_ACCOUNT_ID" \
PLAN_SOURCE_COMMIT="$SOURCE_COMMIT" \
PLAN_ACTIVE_ACCOUNT="$ACTIVE_ACCOUNT" \
PLAN_PROJECT_PARENT_TYPE="$PROJECT_PARENT_TYPE" \
PLAN_PROJECT_PARENT_ID="$PROJECT_PARENT_ID" \
PLAN_PROJECT_NUMBER="$PROJECT_NUMBER" \
PLAN_BILLING_ENABLED="$BILLING_ENABLED" \
PLAN_BILLING_ACCOUNT_NAME="$BILLING_ACCOUNT_NAME" \
PLAN_RELEASE_IDENTITY_EMAIL="$RELEASE_IDENTITY_EMAIL" \
PLAN_RELEASE_IDENTITY_STATE="$RELEASE_IDENTITY_STATE" \
PLAN_MAINTENANCE_IDENTITY_EMAIL="$MAINTENANCE_SERVICE_ACCOUNT" \
PLAN_MAINTENANCE_IDENTITY_STATE="$MAINTENANCE_IDENTITY_STATE" \
PLAN_PROJECT_IAM_PATH="$PROJECT_IAM_PATH" \
PLAN_RELEASE_IAM_PATH="$RELEASE_IAM_PATH" \
PLAN_MAINTENANCE_IAM_PATH="$MAINTENANCE_IAM_PATH" \
PLAN_SLACK_ENABLED="$SLACK_ENABLED" \
PLAN_SLACK_APP_ID="$MIM_SLACK_APP_ID" \
PLAN_SLACK_APPROVED_ORG_ID="$MIM_SLACK_APPROVED_ORG_ID" \
PLAN_SLACK_APPROVED_WORKSPACE_IDS="$MIM_SLACK_APPROVED_WORKSPACE_IDS" \
PLAN_SLACK_REDIRECT_URI="${slack_redirect_uri:-$MIM_TASK18_SLACK_REDIRECT_URI}" \
PLAN_SLACK_BOT_SCOPES_CSV="${slack_bot_scopes_csv:-}" \
PLAN_SLACK_USER_SCOPES_CSV="${slack_user_scopes_csv:-}" \
PLAN_SLACK_ORG_DEPLOY_ENABLED="${slack_org_deploy_enabled:-}" \
PLAN_TENANT_EVIDENCE_HASH="${tenant_evidence_hash:-}" \
PLAN_TENANT_EVIDENCE_APP_ID="${tenant_evidence_app_id:-}" \
PLAN_TENANT_EVIDENCE_ORG_ID="${tenant_evidence_org_id:-}" \
PLAN_TENANT_EVIDENCE_WORKSPACE_IDS_CSV="${tenant_evidence_workspace_ids_csv:-}" \
PLAN_TENANT_EVIDENCE_VERSION="${tenant_evidence_version:-}" \
PLAN_APP_ACCESS_ISSUER="${app_access_issuer:-}" \
PLAN_APP_ACCESS_AUDIENCE="${app_access_audience:-}" \
PLAN_APP_GATEWAY_CURRENT_KEY_ID="${app_gateway_current_key_id:-}" \
PLAN_APP_GATEWAY_PREVIOUS_KEY_ID="${app_gateway_previous_key_id:-}" \
PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION="${app_gateway_previous_secret_version:-}" \
PLAN_APP_GATEWAY_PROOF_SECRET_NAME="$(mim_task18_secret_name_from_ref "$app_gateway_proof_secret_version")" \
PLAN_APP_GATEWAY_PROOF_SECRET_VERSION_NUMBER="$(mim_task18_secret_version_number "$app_gateway_proof_secret_version")" \
PLAN_APP_GATEWAY_PREVIOUS_SECRET_NAME="$(mim_task18_secret_name_from_ref "$app_gateway_previous_secret_version")" \
PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION_NUMBER="$(mim_task18_secret_version_number "$app_gateway_previous_secret_version")" \
PLAN_BLOCKERS_FILE="$BLOCKERS_FILE" \
PLAN_BUILDER_IMAGE_URI="${builder_image_uri:-}" \
PLAN_APP_GATEWAY_IMAGE_URI="${app_gateway_image_uri:-}" \
PLAN_RUNTIME_IMAGE_URI="${runtime_image_uri:-}" \
PLAN_BUILDER_BUILD_ID="${builder_build_id:-}" \
PLAN_APP_GATEWAY_BUILD_ID="${app_gateway_build_id:-}" \
PLAN_RUNTIME_BUILD_ID="${runtime_build_id:-}" \
PLAN_BOOTSTRAP_SECRET_VERSION="${bootstrap_secret_version:-}" \
PLAN_APP_GATEWAY_PROOF_SECRET_VERSION="${app_gateway_proof_secret_version:-}" \
PLAN_BUILDER_BUILD_STATE="$builder_build_state" \
PLAN_APP_GATEWAY_BUILD_STATE="$app_gateway_build_state" \
PLAN_RUNTIME_BUILD_STATE="$runtime_build_state" \
PLAN_BUILDER_ARTIFACT_STATE="$builder_artifact_state" \
PLAN_APP_GATEWAY_ARTIFACT_STATE="$app_gateway_artifact_state" \
PLAN_RUNTIME_ARTIFACT_STATE="$runtime_artifact_state" \
PLAN_BOOTSTRAP_SECRET_STATE="$bootstrap_secret_state" \
PLAN_APP_GATEWAY_PROOF_SECRET_STATE="$app_gateway_proof_secret_state" \
PLAN_BUILDER_BUILD_PATH="$builder_build_path" \
PLAN_APP_GATEWAY_BUILD_PATH="$app_gateway_build_path" \
PLAN_RUNTIME_BUILD_PATH="$runtime_build_path" \
PLAN_BUILDER_ARTIFACT_PATH="$builder_artifact_path" \
PLAN_APP_GATEWAY_ARTIFACT_PATH="$app_gateway_artifact_path" \
PLAN_RUNTIME_ARTIFACT_PATH="$runtime_artifact_path" \
PLAN_BOOTSTRAP_SECRET_PATH="$bootstrap_secret_path" \
PLAN_APP_GATEWAY_PROOF_SECRET_PATH="$app_gateway_proof_secret_path" \
PLAN_CONTROL_PLANE_STATE="$SERVICE_mim_control_plane_STATE" \
PLAN_CONTROL_PLANE_DESCRIBE_PATH="$SERVICE_mim_control_plane_DESCRIBE_PATH" \
PLAN_CONTROL_PLANE_IAM_PATH="$SERVICE_mim_control_plane_IAM_PATH" \
PLAN_APP_GATEWAY_STATE="$SERVICE_mim_app_gateway_STATE" \
PLAN_APP_GATEWAY_DESCRIBE_PATH="$SERVICE_mim_app_gateway_DESCRIBE_PATH" \
PLAN_APP_GATEWAY_IAM_PATH="$SERVICE_mim_app_gateway_IAM_PATH" \
PLAN_DEPLOY_WORKER_STATE="$SERVICE_mim_deploy_worker_STATE" \
PLAN_DEPLOY_WORKER_DESCRIBE_PATH="$SERVICE_mim_deploy_worker_DESCRIBE_PATH" \
PLAN_DEPLOY_WORKER_IAM_PATH="$SERVICE_mim_deploy_worker_IAM_PATH" \
PLAN_SCHEDULE_GATEWAY_STATE="$SERVICE_mim_schedule_gateway_STATE" \
PLAN_SCHEDULE_GATEWAY_DESCRIBE_PATH="$SERVICE_mim_schedule_gateway_DESCRIBE_PATH" \
PLAN_SCHEDULE_GATEWAY_IAM_PATH="$SERVICE_mim_schedule_gateway_IAM_PATH" \
PLAN_JOB_mim_identity_sync_STATE="$JOB_mim_identity_sync_STATE" \
PLAN_JOB_mim_identity_sync_PATH="$JOB_mim_identity_sync_PATH" \
PLAN_JOB_mim_lifecycle_STATE="$JOB_mim_lifecycle_STATE" \
PLAN_JOB_mim_lifecycle_PATH="$JOB_mim_lifecycle_PATH" \
PLAN_JOB_mim_usage_ingest_STATE="$JOB_mim_usage_ingest_STATE" \
PLAN_JOB_mim_usage_ingest_PATH="$JOB_mim_usage_ingest_PATH" \
PLAN_SCHEDULER_mim_identity_sync_STATE="$SCHEDULER_mim_identity_sync_STATE" \
PLAN_SCHEDULER_mim_identity_sync_PATH="$SCHEDULER_mim_identity_sync_PATH" \
PLAN_SCHEDULER_mim_lifecycle_STATE="$SCHEDULER_mim_lifecycle_STATE" \
PLAN_SCHEDULER_mim_lifecycle_PATH="$SCHEDULER_mim_lifecycle_PATH" \
PLAN_SCHEDULER_mim_usage_ingest_STATE="$SCHEDULER_mim_usage_ingest_STATE" \
PLAN_SCHEDULER_mim_usage_ingest_PATH="$SCHEDULER_mim_usage_ingest_PATH" \
python3 - "$PLAN_PATH" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path


def read_json(path_env, fallback):
    path = os.environ.get(path_env, "")
    if not path or not Path(path).exists():
        return fallback
    text = Path(path).read_text().strip()
    if not text:
        return fallback
    return json.loads(text)


def append(blockers, code, message):
    blockers.append({"code": code, "message": message})


def env_list(name):
    return [item for item in os.environ.get(name, "").split(",") if item]


def parse_artifact_digest(payload):
    candidates = [
        payload.get("image_summary", {}).get("fully_qualified_digest"),
        payload.get("image_summary", {}).get("digest"),
        payload.get("fullyQualifiedDigest"),
        payload.get("name"),
    ]
    for value in candidates:
        if isinstance(value, str) and value:
            return value
    return ""


def parse_build(payload, expected_id, expected_repo, expected_name, expected_digest, source_commit, project_id):
    status = payload.get("status")
    build_id = str(payload.get("id", ""))
    service_account = payload.get("serviceAccount")
    substitutions = payload.get("substitutions") or {}
    images = ((payload.get("results") or {}).get("images")) or []
    expected_tag = f"asia-northeast3-docker.pkg.dev/{project_id}/{expected_repo}/{expected_name}:git-{source_commit}"
    expected_service_account = f"projects/{project_id}/serviceAccounts/mim-build@{project_id}.iam.gserviceaccount.com"
    matches = []
    for item in images:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        digest = item.get("digest")
        if name == expected_tag:
            matches.append({"name": name, "digest": digest})
    return {
        "id": build_id,
        "status": status,
        "service_account": service_account,
        "source_commit": substitutions.get("_MIM_SOURCE_COMMIT"),
        "expected_id": expected_id,
        "expected_tag": expected_tag,
        "expected_service_account": expected_service_account,
        "matching_results": matches,
        "reviewed_digest_uri": expected_digest,
    }


def parse_service(describe_payload, iam_payload, state, desired):
    if state != "exists":
        return {
            "state": state,
            "service_account": None,
            "image_uri": None,
            "runtime_mode": None,
            "env": {},
            "secret_env": {},
            "ingress": None,
            "cpu": None,
            "memory": None,
            "concurrency": None,
            "timeout_seconds": None,
            "service_min_scale": None,
            "service_max_scale": None,
            "min_scale": None,
            "max_scale": None,
            "cpu_throttling": None,
            "startup_cpu_boost": None,
            "invoker_members": [],
        }
    template = ((describe_payload.get("spec") or {}).get("template")) or {}
    metadata = template.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    spec = template.get("spec") or {}
    service_annotations = (describe_payload.get("metadata") or {}).get("annotations") or {}
    containers = spec.get("containers") or [{}]
    container = containers[0] if containers else {}
    limits = ((container.get("resources") or {}).get("limits")) or {}
    env_entries = container.get("env") or []
    env_map = {}
    secret_env_map = {}
    for item in env_entries:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            name = item["name"]
            if "value" in item:
                env_map[name] = item.get("value")
                continue
            value_from = item.get("valueFrom") or item.get("value_source") or {}
            secret_ref = value_from.get("secretKeyRef") or value_from.get("secret_key_ref") or {}
            secret_name = secret_ref.get("name")
            secret_key = secret_ref.get("key")
            if isinstance(secret_name, str) and isinstance(secret_key, str):
                secret_env_map[name] = f"{secret_name}:{secret_key}"
    members = []
    for binding in (iam_payload.get("bindings") or []):
        if not isinstance(binding, dict) or binding.get("role") != "roles/run.invoker":
            continue
        for member in binding.get("members") or []:
            if isinstance(member, str):
                members.append(member)
    return {
        "state": state,
        "service_min_scale": service_annotations.get("run.googleapis.com/minScale"),
        "service_max_scale": service_annotations.get("run.googleapis.com/maxScale"),
        "service_account": spec.get("serviceAccountName"),
        "image_uri": container.get("image"),
        "runtime_mode": env_map.get("MIM_RUNTIME_MODE"),
        "env": env_map,
        "secret_env": secret_env_map,
        "ingress": annotations.get("run.googleapis.com/ingress"),
        "cpu": limits.get("cpu"),
        "memory": limits.get("memory"),
        "concurrency": spec.get("containerConcurrency"),
        "timeout_seconds": spec.get("timeoutSeconds"),
        "min_scale": annotations.get("autoscaling.knative.dev/minScale"),
        "max_scale": annotations.get("autoscaling.knative.dev/maxScale"),
        "cpu_throttling": annotations.get("run.googleapis.com/cpu-throttling"),
        "startup_cpu_boost": annotations.get("run.googleapis.com/startup-cpu-boost"),
        "invoker_members": sorted(set(members)),
    }


def desired_service(project_id, runtime_image, bootstrap_secret, name, runtime_mode, allow_unauth, ingress, plain_env=None, secret_env=None, expected_invoker_members=None):
    service_account = f"{name}@{project_id}.iam.gserviceaccount.com"
    if expected_invoker_members is None:
        if allow_unauth:
            expected_invoker_members = ["allUsers"]
        else:
            expected_invoker_members = [f"serviceAccount:{service_account}"]
    env = {
        "MIM_RUNTIME_MODE": runtime_mode,
        "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": bootstrap_secret,
        "MIM_ENABLE_MUTATIONS": "true",
    }
    if plain_env is not None:
        env = plain_env
    return {
        "name": name,
        "runtime_mode": runtime_mode,
        "service_account": service_account,
        "image_uri": runtime_image,
        "allow_unauthenticated": allow_unauth,
        "ingress": ingress,
        "cpu": "1",
        "memory": "512Mi",
        "concurrency": 20,
        "timeout_seconds": 300,
        "service_min_scale": "0",
        "service_max_scale": "1",
        "min_scale": "0",
        "max_scale": "1",
        "cpu_throttling": "true",
        "startup_cpu_boost": "false",
        "env": env,
        "secret_env": secret_env or {},
        "expected_invoker_members": expected_invoker_members,
    }


def service_matches(current, desired):
    if current.get("state") != "exists":
        return False
    fields = (
        "service_account",
        "image_uri",
        "ingress",
        "cpu",
        "memory",
        "concurrency",
        "timeout_seconds",
        "service_min_scale",
        "service_max_scale",
        "min_scale",
        "max_scale",
        "cpu_throttling",
        "startup_cpu_boost",
    )
    for field in fields:
        if current.get(field) != desired.get(field):
            return False
    return current.get("env") == desired.get("env") and current.get("secret_env") == desired.get("secret_env")


def desired_job(project_id, runtime_image, bootstrap_secret, maintenance_sa, name, runtime_mode, module):
    return {
        "name": name,
        "service_account": maintenance_sa,
        "image_uri": runtime_image,
        "command": ["python", "-m", module],
        "env": {
            "MIM_RUNTIME_MODE": runtime_mode,
            "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": bootstrap_secret,
            "MIM_ENABLE_MUTATIONS": "true",
        },
        "tasks": 1,
        "parallelism": 1,
        "max_retries": 0,
        "timeout": "600s",
        "cpu": "1",
        "memory": "512Mi",
    }


def parse_job(payload, state):
    if state != "exists":
        return {
            "state": state,
            "service_account": None,
            "image_uri": None,
            "command": [],
            "env": {},
            "tasks": None,
            "parallelism": None,
            "max_retries": None,
            "timeout": None,
            "cpu": None,
            "memory": None,
        }
    template = payload.get("template") or {}
    execution = template.get("template") or {}
    containers = execution.get("containers") or [{}]
    container = containers[0] if containers else {}
    env_entries = container.get("env") or []
    env_map = {}
    for item in env_entries:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            env_map[item["name"]] = item.get("value")
    limits = ((container.get("resources") or {}).get("limits")) or {}
    command = []
    for item in container.get("command") or []:
        if isinstance(item, str):
            command.append(item)
    for item in container.get("args") or []:
        if isinstance(item, str):
            command.append(item)
    timeout = execution.get("timeout")
    if timeout is None and execution.get("timeoutSeconds") is not None:
        timeout = f"{execution.get('timeoutSeconds')}s"
    return {
        "state": state,
        "service_account": execution.get("serviceAccount") or execution.get("serviceAccountName"),
        "image_uri": container.get("image"),
        "command": command,
        "env": env_map,
        "tasks": template.get("taskCount"),
        "parallelism": template.get("parallelism"),
        "max_retries": execution.get("maxRetries"),
        "timeout": timeout,
        "cpu": limits.get("cpu"),
        "memory": limits.get("memory"),
    }


def job_matches(current, desired):
    if current.get("state") != "exists":
        return False
    fields = ("service_account", "image_uri", "command", "env", "tasks", "parallelism", "max_retries", "timeout", "cpu", "memory")
    for field in fields:
        if current.get(field) != desired.get(field):
            return False
    return True


def desired_scheduler(project_id, region, maintenance_sa, name, schedule):
    return {
        "name": name,
        "schedule": schedule,
        "time_zone": "UTC",
        "uri": f"https://run.googleapis.com/v2/projects/{project_id}/locations/{region}/jobs/{name}:run",
        "http_method": "POST",
        "oauth_service_account": maintenance_sa,
    }


def parse_scheduler(payload, state):
    if state != "exists":
        return {
            "state": state,
            "schedule": None,
            "time_zone": None,
            "uri": None,
            "http_method": None,
            "oauth_service_account": None,
        }
    target = payload.get("httpTarget") or {}
    oauth = target.get("oauthToken") or {}
    return {
        "state": state,
        "schedule": payload.get("schedule"),
        "time_zone": payload.get("timeZone"),
        "uri": target.get("uri"),
        "http_method": target.get("httpMethod"),
        "oauth_service_account": oauth.get("serviceAccountEmail"),
    }


def scheduler_matches(current, desired):
    if current.get("state") != "exists":
        return False
    fields = ("schedule", "time_zone", "uri", "http_method", "oauth_service_account")
    for field in fields:
        if current.get(field) != desired.get(field):
            return False
    return True


def binding_key(binding):
    condition = binding.get("condition") or {}
    title = condition.get("title") if isinstance(condition, dict) else None
    expression = condition.get("expression") if isinstance(condition, dict) else None
    return (binding.get("role"), title, expression)


def normalized_members(binding):
    return sorted({member for member in binding.get("members") or [] if isinstance(member, str)})


def parse_project_iam(project_payload, blockers, project_id, region, release_email, maintenance_email, operator_email):
    bindings = [item for item in project_payload.get("bindings") or [] if isinstance(item, dict)]
    release_member = f"serviceAccount:{release_email}"
    maintenance_member = f"serviceAccount:{maintenance_email}"
    release_job_expression = (
        f'resource.name == "projects/{project_id}/locations/{region}/services/mim-control-plane" || '
        f'resource.name == "projects/{project_id}/locations/{region}/services/mim-app-gateway" || '
        f'resource.name == "projects/{project_id}/locations/{region}/services/mim-deploy-worker" || '
        f'resource.name == "projects/{project_id}/locations/{region}/services/mim-schedule-gateway" || '
        f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-identity-sync" || '
        f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-lifecycle" || '
        f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-usage-ingest"'
    )
    maintenance_expression = (
        f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-identity-sync" || '
        f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-lifecycle" || '
        f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-usage-ingest"'
    )
    expected_release = {
        ("roles/run.admin", "mim-release-runtimes", release_job_expression),
        ("roles/cloudscheduler.admin", None, None),
    }
    observed_release = set()
    project_run_invoker_members = []
    maintenance_executor_exact = False
    for binding in bindings:
        members = normalized_members(binding)
        key = binding_key(binding)
        if binding.get("role") == "roles/run.invoker":
            project_run_invoker_members.extend(members)
        if release_member in members:
            observed_release.add(key)
        if binding.get("role") == "roles/run.jobsExecutor" and maintenance_member in members:
            if key == ("roles/run.jobsExecutor", "mim-fixed-maintenance-jobs", maintenance_expression) and members == [maintenance_member]:
                maintenance_executor_exact = True
    if project_run_invoker_members:
        append(blockers, "project-run-invoker-drift", "Project-wide Cloud Run invoker bindings are forbidden.")
    if observed_release != expected_release:
        append(blockers, "release-project-role-drift", "Release identity project roles must match the reviewed conditional contract.")
    if not maintenance_executor_exact:
        append(blockers, "maintenance-job-executor-drift", "Maintenance identity must hold the exact reviewed Cloud Run job execution role.")
    return {
        "release_member": release_member,
        "maintenance_member": maintenance_member,
        "project_run_invoker_members": sorted(set(project_run_invoker_members)),
        "expected_release_bindings": [
            {"role": role, "title": title, "expression": expression}
            for role, title, expression in sorted(expected_release)
        ],
        "observed_release_bindings": [
            {"role": role, "title": title, "expression": expression}
            for role, title, expression in sorted(observed_release)
        ],
        "maintenance_executor_exact": maintenance_executor_exact,
    }


def parse_service_account_policy(payload, expected_role, expected_member):
    bindings = [item for item in payload.get("bindings") or [] if isinstance(item, dict)]
    observed = {}
    for binding in bindings:
        observed[str(binding.get("role", ""))] = normalized_members(binding)
    return observed.get(expected_role) == [expected_member], observed


blockers = []
for raw in Path(os.environ["PLAN_BLOCKERS_FILE"]).read_text().splitlines():
    if raw:
        code, message = raw.split("\t", 1)
        blockers.append({"code": code, "message": message})

project_id = os.environ["PLAN_PROJECT_ID"]
region = "asia-northeast3"
source_commit = os.environ["PLAN_SOURCE_COMMIT"]
builder_image_uri = os.environ["PLAN_BUILDER_IMAGE_URI"]
app_gateway_image_uri = os.environ["PLAN_APP_GATEWAY_IMAGE_URI"]
runtime_image_uri = os.environ["PLAN_RUNTIME_IMAGE_URI"]
builder_build_id = os.environ["PLAN_BUILDER_BUILD_ID"]
app_gateway_build_id = os.environ["PLAN_APP_GATEWAY_BUILD_ID"]
runtime_build_id = os.environ["PLAN_RUNTIME_BUILD_ID"]
bootstrap_secret_version = os.environ["PLAN_BOOTSTRAP_SECRET_VERSION"]
app_gateway_proof_secret_version = os.environ["PLAN_APP_GATEWAY_PROOF_SECRET_VERSION"]
release_identity_email = os.environ["PLAN_RELEASE_IDENTITY_EMAIL"]
maintenance_identity_email = os.environ["PLAN_MAINTENANCE_IDENTITY_EMAIL"]
operator_email = os.environ["PLAN_OPERATOR_EMAIL"]

builder_build = parse_build(
    read_json("PLAN_BUILDER_BUILD_PATH", {}),
    builder_build_id,
    "mim-platform",
    "mim-builder",
    builder_image_uri,
    source_commit,
    project_id,
)
runtime_build = parse_build(
    read_json("PLAN_RUNTIME_BUILD_PATH", {}),
    runtime_build_id,
    "mim-control-plane",
    "runtime",
    runtime_image_uri,
    source_commit,
    project_id,
)
app_gateway_build = parse_build(
    read_json("PLAN_APP_GATEWAY_BUILD_PATH", {}),
    app_gateway_build_id,
    "mim-platform",
    "app-gateway",
    app_gateway_image_uri,
    source_commit,
    project_id,
)
builder_artifact = {
    "state": os.environ["PLAN_BUILDER_ARTIFACT_STATE"],
    "digest_uri": parse_artifact_digest(read_json("PLAN_BUILDER_ARTIFACT_PATH", {})),
}
app_gateway_artifact = {
    "state": os.environ["PLAN_APP_GATEWAY_ARTIFACT_STATE"],
    "digest_uri": parse_artifact_digest(read_json("PLAN_APP_GATEWAY_ARTIFACT_PATH", {})),
}
runtime_artifact = {
    "state": os.environ["PLAN_RUNTIME_ARTIFACT_STATE"],
    "digest_uri": parse_artifact_digest(read_json("PLAN_RUNTIME_ARTIFACT_PATH", {})),
}
bootstrap_secret = read_json("PLAN_BOOTSTRAP_SECRET_PATH", {})
bootstrap_state = {
    "state": os.environ["PLAN_BOOTSTRAP_SECRET_STATE"],
    "name": bootstrap_secret.get("name"),
    "status": bootstrap_secret.get("state"),
}
app_gateway_proof_secret = read_json("PLAN_APP_GATEWAY_PROOF_SECRET_PATH", {})
app_gateway_proof_secret_state = {
    "state": os.environ["PLAN_APP_GATEWAY_PROOF_SECRET_STATE"],
    "name": app_gateway_proof_secret.get("name"),
    "status": app_gateway_proof_secret.get("state"),
}

for env_prefix, label, build in (
    ("BUILDER", "builder", builder_build),
    ("APP_GATEWAY", "app-gateway", app_gateway_build),
    ("RUNTIME", "runtime", runtime_build),
):
    state = os.environ[f"PLAN_{env_prefix}_BUILD_STATE"]
    if state == "exists":
        if build["id"] != build["expected_id"]:
            append(blockers, f"{label}-build-id-mismatch", f"Reviewed {label} Cloud Build ID does not match the fetched build evidence.")
        if build["status"] != "SUCCESS":
            append(blockers, f"{label}-build-status-mismatch", f"Reviewed {label} Cloud Build must be SUCCESS.")
        if build["service_account"] != build["expected_service_account"]:
            append(blockers, f"{label}-build-service-account-mismatch", f"Reviewed {label} Cloud Build must run as the exact mim-build service account.")
        if build["source_commit"] != source_commit:
            append(blockers, f"{label}-build-source-commit-mismatch", f"Reviewed {label} Cloud Build must target the current source commit.")
        if len(build["matching_results"]) != 1:
            append(blockers, f"{label}-build-result-mismatch", f"Reviewed {label} Cloud Build must publish exactly one matching git-tagged image result.")
        else:
            digest = build["matching_results"][0].get("digest")
            if build["reviewed_digest_uri"] != f"{build['expected_tag'].split(':git-')[0]}@{digest}":
                append(blockers, f"{label}-build-digest-mismatch", f"Reviewed {label} Cloud Build digest does not match the reviewed Artifact Registry digest URI.")

for label, artifact, expected in (
    ("builder", builder_artifact, builder_image_uri),
    ("app-gateway", app_gateway_artifact, app_gateway_image_uri),
    ("runtime", runtime_artifact, runtime_image_uri),
):
    if artifact["state"] == "exists" and artifact["digest_uri"] != expected:
        append(blockers, f"{label}-artifact-digest-mismatch", f"Reviewed {label} Artifact Registry digest does not match the reviewed URI.")

if bootstrap_state["state"] == "exists":
    if bootstrap_state["name"] != bootstrap_secret_version:
        append(blockers, "runtime-bootstrap-secret-version-mismatch", "Reviewed runtime bootstrap secret version path does not match the fetched Secret Manager version.")
    if bootstrap_state["status"] != "ENABLED":
        append(blockers, "runtime-bootstrap-secret-version-disabled", "Reviewed runtime bootstrap secret version must be ENABLED.")

if app_gateway_proof_secret_state["state"] == "exists":
    if app_gateway_proof_secret_state["name"] != app_gateway_proof_secret_version:
        append(blockers, "app-gateway-proof-secret-version-mismatch", "Reviewed app-gateway proof secret version path does not match the fetched Secret Manager version.")
    if app_gateway_proof_secret_state["status"] != "ENABLED":
        append(blockers, "app-gateway-proof-secret-version-disabled", "Reviewed app-gateway proof secret version must be ENABLED.")

actions = []
gateway_plain_env = {
    "MIM_PUBLIC_SUFFIX": "madup.app",
    "MIM_PROJECT_ID": project_id,
    "MIM_PROJECT_NUMBER": os.environ["PLAN_PROJECT_NUMBER"],
    "MIM_REGION": region,
    "MIM_CLOUDFLARE_ACCESS_ISSUER": os.environ["PLAN_APP_ACCESS_ISSUER"],
    "MIM_CLOUDFLARE_ACCESS_AUDIENCE": os.environ["PLAN_APP_ACCESS_AUDIENCE"],
    "MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL": f"mim-app-gateway@{project_id}.iam.gserviceaccount.com",
    "MIM_APP_AUTHORIZATION_URL": f"https://mim-schedule-gateway-{os.environ['PLAN_PROJECT_NUMBER']}.{region}.run.app/v1/apps/authorize",
    "MIM_APP_AUTHORIZATION_AUDIENCE": f"https://mim-schedule-gateway-{os.environ['PLAN_PROJECT_NUMBER']}.{region}.run.app",
    "MIM_APP_PROOF_CURRENT_KEY_ID": os.environ["PLAN_APP_GATEWAY_CURRENT_KEY_ID"],
}
gateway_secret_env = {
    "MIM_APP_PROOF_CURRENT_SECRET": f"{os.environ['PLAN_APP_GATEWAY_PROOF_SECRET_NAME']}:{os.environ['PLAN_APP_GATEWAY_PROOF_SECRET_VERSION_NUMBER']}",
}
if os.environ.get("PLAN_APP_GATEWAY_PREVIOUS_KEY_ID"):
    gateway_plain_env["MIM_APP_PROOF_PREVIOUS_KEY_ID"] = os.environ["PLAN_APP_GATEWAY_PREVIOUS_KEY_ID"]
    gateway_secret_env["MIM_APP_PROOF_PREVIOUS_SECRET"] = f"{os.environ['PLAN_APP_GATEWAY_PREVIOUS_SECRET_NAME']}:{os.environ['PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION_NUMBER']}"
desired_services = [
    desired_service(project_id, runtime_image_uri, bootstrap_secret_version, "mim-control-plane", "control-plane", True, "all"),
    desired_service(project_id, app_gateway_image_uri, bootstrap_secret_version, "mim-app-gateway", "app-gateway", True, "all", gateway_plain_env, gateway_secret_env),
    desired_service(project_id, runtime_image_uri, bootstrap_secret_version, "mim-deploy-worker", "deploy-worker", False, "internal"),
    desired_service(
        project_id,
        runtime_image_uri,
        bootstrap_secret_version,
        "mim-schedule-gateway",
        "schedule-gateway",
        False,
        "internal",
        expected_invoker_members=[
            f"serviceAccount:{maintenance_identity_email}",
            f"serviceAccount:mim-app-gateway@{project_id}.iam.gserviceaccount.com",
        ],
    ),
]

current_services = []
for env_prefix, desired in (
    ("CONTROL_PLANE", desired_services[0]),
    ("APP_GATEWAY", desired_services[1]),
    ("DEPLOY_WORKER", desired_services[2]),
    ("SCHEDULE_GATEWAY", desired_services[3]),
):
    current = parse_service(
        read_json(f"PLAN_{env_prefix}_DESCRIBE_PATH", {}),
        read_json(f"PLAN_{env_prefix}_IAM_PATH", {}),
        os.environ[f"PLAN_{env_prefix}_STATE"],
        desired,
    )
    current["name"] = desired["name"]
    current_services.append(current)
    if not service_matches(current, desired):
        actions.append({
            "kind": f"deploy_{desired['runtime_mode'].replace('-', '_')}",
            "name": desired["name"],
            "runtime_mode": desired["runtime_mode"],
            "service_account": desired["service_account"],
            "image_uri": desired["image_uri"],
            "allow_unauthenticated": desired["allow_unauthenticated"],
            "ingress": desired["ingress"],
            "expected_invoker_members": desired["expected_invoker_members"],
        })
    invokers = current["invoker_members"]
    expected_members = sorted(desired["expected_invoker_members"])
    unexpected = [member for member in invokers if member not in expected_members]
    if unexpected:
        append(blockers, f"{desired['runtime_mode']}-invoker-drift", f"{desired['name']} has unexpected Cloud Run invoker bindings.")
    for expected_member in expected_members:
        if expected_member not in invokers:
            actions.append({
                "kind": f"bind_invoker_{desired['runtime_mode'].replace('-', '_')}",
                "name": desired["name"],
                "member": expected_member,
            })

desired_jobs = [
    desired_job(project_id, runtime_image_uri, bootstrap_secret_version, maintenance_identity_email, "mim-identity-sync", "identity-sync", "mim_control_plane.jobs.directory_sync"),
    desired_job(project_id, runtime_image_uri, bootstrap_secret_version, maintenance_identity_email, "mim-lifecycle", "lifecycle", "mim_control_plane.jobs.lifecycle"),
    desired_job(project_id, runtime_image_uri, bootstrap_secret_version, maintenance_identity_email, "mim-usage-ingest", "usage-ingest", "mim_control_plane.jobs.usage_ingest"),
]
desired_schedulers = [
    desired_scheduler(project_id, region, maintenance_identity_email, "mim-identity-sync", "*/15 * * * *"),
    desired_scheduler(project_id, region, maintenance_identity_email, "mim-lifecycle", "7,22,37,52 * * * *"),
    desired_scheduler(project_id, region, maintenance_identity_email, "mim-usage-ingest", "12 * * * *"),
]

current_jobs = []
for desired in desired_jobs:
    slug = desired["name"].replace("-", "_")
    current = parse_job(read_json(f"PLAN_JOB_{slug}_PATH", {}), os.environ[f"PLAN_JOB_{slug}_STATE"])
    current["name"] = desired["name"]
    current_jobs.append(current)
    if not job_matches(current, desired):
        actions.append({"kind": "upsert_job", "name": desired["name"], "module": desired["command"][2]})

current_schedulers = []
for desired in desired_schedulers:
    slug = desired["name"].replace("-", "_")
    current = parse_scheduler(read_json(f"PLAN_SCHEDULER_{slug}_PATH", {}), os.environ[f"PLAN_SCHEDULER_{slug}_STATE"])
    current["name"] = desired["name"]
    current_schedulers.append(current)
    if not scheduler_matches(current, desired):
        mode = "create" if current.get("state") != "exists" else "update"
        actions.append({"kind": "upsert_scheduler", "name": desired["name"], "mode": mode})

project_iam = read_json("PLAN_PROJECT_IAM_PATH", {})
release_iam = read_json("PLAN_RELEASE_IAM_PATH", {})
maintenance_iam = read_json("PLAN_MAINTENANCE_IAM_PATH", {})
iam_state = parse_project_iam(
    project_iam,
    blockers,
    project_id,
    region,
    release_identity_email,
    maintenance_identity_email,
    operator_email,
)
release_policy_exact, release_policy_observed = parse_service_account_policy(
    release_iam,
    "roles/iam.serviceAccountTokenCreator",
    f"user:{operator_email}",
)
if not release_policy_exact:
    append(blockers, "release-token-creator-drift", "Release identity must grant the exact operator token creation binding.")
maintenance_policy_exact, maintenance_policy_observed = parse_service_account_policy(
    maintenance_iam,
    "roles/iam.serviceAccountUser",
    f"serviceAccount:{release_identity_email}",
)
if not maintenance_policy_exact:
    append(blockers, "maintenance-actas-drift", "Maintenance identity must grant release the exact actAs binding.")

approved_workspace_ids = env_list("PLAN_SLACK_APPROVED_WORKSPACE_IDS")
bot_scopes = env_list("PLAN_SLACK_BOT_SCOPES_CSV")
user_scopes = env_list("PLAN_SLACK_USER_SCOPES_CSV")
tenant_workspace_ids = env_list("PLAN_TENANT_EVIDENCE_WORKSPACE_IDS_CSV")
slack_enabled = os.environ["PLAN_SLACK_ENABLED"] == "true"

initial_state = {
    "active_account": os.environ["PLAN_ACTIVE_ACCOUNT"],
    "project": {
        "project_id": project_id,
        "parent_type": os.environ["PLAN_PROJECT_PARENT_TYPE"],
        "parent_id": os.environ["PLAN_PROJECT_PARENT_ID"],
        "project_number": os.environ["PLAN_PROJECT_NUMBER"],
        "billing_enabled": os.environ["PLAN_BILLING_ENABLED"] == "True",
        "billing_account_name": os.environ["PLAN_BILLING_ACCOUNT_NAME"],
    },
    "release_identity": {
        "status": os.environ["PLAN_RELEASE_IDENTITY_STATE"],
        "email": release_identity_email,
    },
    "maintenance_identity": {
        "status": os.environ["PLAN_MAINTENANCE_IDENTITY_STATE"],
        "email": maintenance_identity_email,
    },
    "slack": {
        "enabled": slack_enabled,
    },
    "provenance": {
        "builder": builder_build,
        "app_gateway": app_gateway_build,
        "runtime": runtime_build,
        "builder_artifact": builder_artifact,
        "runtime_artifact": runtime_artifact,
        "runtime_bootstrap_secret_version": bootstrap_state,
    },
    "runtime_services": current_services,
    "runtime_jobs": current_jobs,
    "schedulers": current_schedulers,
    "iam": {
        "status": "ready" if not any(item["code"] in {"project-run-invoker-drift", "release-project-role-drift", "maintenance-job-executor-drift", "release-token-creator-drift", "maintenance-actas-drift"} for item in blockers) else "blocked",
        "project_run_invoker_members": iam_state["project_run_invoker_members"],
        "expected_release_bindings": iam_state["expected_release_bindings"],
        "observed_release_bindings": iam_state["observed_release_bindings"],
        "maintenance_executor_exact": iam_state["maintenance_executor_exact"],
        "release_service_account_policy": release_policy_observed,
        "maintenance_service_account_policy": maintenance_policy_observed,
    },
}
if slack_enabled:
    initial_state["slack_app"] = {
        "app_id": os.environ["PLAN_SLACK_APP_ID"],
        "approved_org_id": os.environ["PLAN_SLACK_APPROVED_ORG_ID"],
        "approved_workspace_ids": approved_workspace_ids,
        "redirect_uri": os.environ["PLAN_SLACK_REDIRECT_URI"],
        "bot_scopes": bot_scopes,
        "user_scopes": user_scopes,
        "org_deploy_enabled": os.environ["PLAN_SLACK_ORG_DEPLOY_ENABLED"] == "true",
        "tenant_allowlist_observable": False,
        "tenant_allowlist_source": "reviewed_tenant_evidence",
    }
    initial_state["slack_tenant_allowlist_evidence"] = {
        "schema_version": os.environ["PLAN_TENANT_EVIDENCE_VERSION"],
        "evidence_hash": os.environ["PLAN_TENANT_EVIDENCE_HASH"],
        "app_id": os.environ["PLAN_TENANT_EVIDENCE_APP_ID"],
        "approved_org_id": os.environ["PLAN_TENANT_EVIDENCE_ORG_ID"],
        "approved_workspace_ids": tenant_workspace_ids,
    }
discovery_hash = hashlib.sha256(json.dumps(initial_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

plan = {
    "version": "mim-release-plan-v6",
    "generated_at_epoch": int(os.environ["PLAN_GENERATED_AT"]),
    "expires_at_epoch": int(os.environ["PLAN_EXPIRES_AT"]),
    "status": "blocked" if blockers else "ready",
    "blockers": blockers,
    "config": {
        "operator_email": operator_email,
        "project_id": project_id,
        "organization_id": os.environ["PLAN_ORGANIZATION_ID"],
        "billing_account_id": os.environ["PLAN_BILLING_ACCOUNT_ID"],
        "config_fingerprint": os.environ["PLAN_CONFIG_FINGERPRINT"],
    },
    "targets": {
        "source_repository": "madup-dct/claude-plugins",
        "source_commit": source_commit,
        "builder_image_uri": builder_image_uri,
        "builder_build_id": builder_build_id,
        "app_gateway_image_uri": app_gateway_image_uri,
        "app_gateway_build_id": app_gateway_build_id,
        "runtime_image_uri": runtime_image_uri,
        "runtime_build_id": runtime_build_id,
        "runtime_bootstrap_secret_version": bootstrap_secret_version,
        "app_gateway_proof_secret_version": app_gateway_proof_secret_version,
        "project_number": os.environ["PLAN_PROJECT_NUMBER"],
        "app_access_issuer": os.environ["PLAN_APP_ACCESS_ISSUER"],
        "app_access_audience": os.environ["PLAN_APP_ACCESS_AUDIENCE"],
        "app_gateway_current_key_id": os.environ["PLAN_APP_GATEWAY_CURRENT_KEY_ID"],
        "app_gateway_previous_key_id": os.environ.get("PLAN_APP_GATEWAY_PREVIOUS_KEY_ID", ""),
        "app_gateway_previous_secret_version": os.environ.get("PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION", ""),
        "region": region,
    },
    "initial_state": initial_state,
    "constraints": {
        "mutations_require_exact_true_env": "MIM_ENABLE_MUTATIONS",
        "reviewed_commit_env": "MIM_TASK18_REVIEWED_PLATFORM_COMMIT",
        "mutation_operator_email": operator_email,
        "mutation_impersonation_service_account": release_identity_email,
        "maintenance_service_account": maintenance_identity_email,
        "fixed_region": region,
        "job_bounds": {
            "tasks": 1,
            "parallelism": 1,
            "max_retries": 0,
            "timeout": "600s",
            "cpu": "1",
            "memory": "512Mi",
        },
        "scheduler_time_zone": "UTC",
    },
    "desired_services": desired_services,
    "desired_jobs": desired_jobs,
    "desired_schedulers": desired_schedulers,
    "actions": actions,
    "required_secrets": [bootstrap_secret_version, app_gateway_proof_secret_version],
    "required_apis": [],
    "discovery_hash": discovery_hash,
}
Path(sys.argv[1]).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
PY

mim_task18_write_plan_json "$PLAN_PATH" "$PLAN_OUT"
printf 'Wrote reviewed plan to %s\n' "$PLAN_OUT"
