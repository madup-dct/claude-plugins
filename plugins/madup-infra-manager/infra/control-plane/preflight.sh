#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=config_lib.sh
. "$SCRIPT_DIR/config_lib.sh"
# shellcheck source=iam/lib.sh
. "$SCRIPT_DIR/iam/lib.sh"

CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_default_config_file "$SCRIPT_DIR")}"
PROTECTED_PROJECTS_FILE="${MIM_PROTECTED_PROJECTS_FILE:-$(mim_default_protected_projects_file "$SCRIPT_DIR")}"
TMP_DIR=$(mktemp -d)
trap 'rm -rf -- "$TMP_DIR"' EXIT

mim_require_no_args "$@"
mim_load_config "$CONFIG_FILE"
mim_assert_project_not_protected "$MIM_PROJECT_ID" "$PROTECTED_PROJECTS_FILE"
command -v gcloud >/dev/null 2>&1 || mim_fail "gcloud CLI is required"
command -v bq >/dev/null 2>&1 || mim_fail "bq CLI is required"
command -v python3 >/dev/null 2>&1 || mim_fail "python3 is required"

gcloud_capture() {
  local description=$1
  shift
  local output
  if ! output=$(gcloud "$@" 2>/dev/null); then
    mim_fail "$description"
  fi
  printf '%s' "$output"
}

gcloud_optional_state() {
  local description=$1
  local output_file=$2
  shift 2

  local stderr_file
  local status
  stderr_file=$(mktemp)
  set +e
  gcloud "$@" >"$output_file" 2>"$stderr_file"
  status=$?
  set -e

  if [[ "$status" -eq 0 ]]; then
    rm -f -- "$stderr_file"
    printf 'exists'
    return
  fi

  if grep -Fq -- 'Cannot find service' "$stderr_file" || grep -Fq -- 'NOT_FOUND:' "$stderr_file"; then
    rm -f -- "$stderr_file"
    : >"$output_file"
    printf 'missing'
    return
  fi

  cat "$stderr_file" >/dev/null
  rm -f -- "$stderr_file"
  mim_fail "$description"
}

ACTIVE_ACCOUNT=$(gcloud_capture \
  "Unable to determine the active gcloud account" \
  auth list \
  --filter=status:ACTIVE \
  '--format=value(account)' \
  --account="$MIM_OPERATOR_EMAIL")
[[ "$ACTIVE_ACCOUNT" == "$MIM_OPERATOR_EMAIL" ]] || mim_fail "Active gcloud account does not match the configured operator"

PROJECT_ID=$(gcloud_capture \
  "Unable to describe the configured project" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(projectId)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_ID" == "$MIM_PROJECT_ID" ]] || mim_fail "Configured project mismatch"

PROJECT_PARENT_TYPE=$(gcloud_capture \
  "Unable to determine the project parent type" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(parent.type)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_PARENT_TYPE" == "organization" ]] || mim_fail "Project parent must be an organization"

PROJECT_PARENT_ID=$(gcloud_capture \
  "Unable to determine the project organization" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(parent.id)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_PARENT_ID" == "$MIM_ORGANIZATION_ID" ]] || mim_fail "Project organization mismatch"

PROJECT_NUMBER=$(gcloud_capture \
  "Unable to determine the project number" \
  projects describe "$MIM_PROJECT_ID" \
  '--format=value(projectNumber)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || mim_fail "Invalid project number returned"

BILLING_ENABLED=$(gcloud_capture \
  "Unable to determine project billing status" \
  billing projects describe "$MIM_PROJECT_ID" \
  '--format=value(billingEnabled)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$BILLING_ENABLED" == "True" ]] || mim_fail "Billing must be linked"

BILLING_ACCOUNT_NAME=$(gcloud_capture \
  "Unable to determine the billing account" \
  billing projects describe "$MIM_PROJECT_ID" \
  '--format=value(billingAccountName)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ "$BILLING_ACCOUNT_NAME" == "billingAccounts/$MIM_BILLING_ACCOUNT_ID" ]] || mim_fail "Billing account mismatch"

ENABLED_APIS=$(gcloud_capture \
  "Unable to list enabled APIs" \
  services list \
  --enabled \
  '--format=value(config.name)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
: "$ENABLED_APIS"

PROJECT_RUN_INVOKERS=$(gcloud_capture \
  "Unable to inspect project-wide Cloud Run invoker bindings" \
  projects get-iam-policy "$MIM_PROJECT_ID" \
  '--flatten=bindings[].members' \
  '--filter=bindings.role=roles/run.invoker' \
  '--format=value(bindings.members)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
[[ -z "$PROJECT_RUN_INVOKERS" ]] || mim_fail "Project-wide Cloud Run invoker bindings are forbidden"

mim_capture_iam_contract "$SCRIPT_DIR" "$TMP_DIR" "$PROJECT_NUMBER"
if iam_issue=$(mim_iam_first_issue); then
  mim_fail "$iam_issue"
fi

RUN_STATE_FILE=$(mktemp)
RUN_SERVICE_STATE=$(gcloud_optional_state \
  "Unable to inspect the control-plane service" \
  "$RUN_STATE_FILE" \
  run services describe "$MIM_CONTROL_PLANE_SERVICE" \
  --region="$MIM_FIXED_REGION" \
  '--format=value(metadata.name)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")

if [[ "$RUN_SERVICE_STATE" == "exists" ]]; then
  RUN_SERVICE_ACCOUNT=$(gcloud_capture \
    "Unable to inspect the control-plane runtime identity" \
    run services describe "$MIM_CONTROL_PLANE_SERVICE" \
    --region="$MIM_FIXED_REGION" \
    '--format=value(spec.template.spec.serviceAccountName)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$RUN_SERVICE_ACCOUNT" == "$(mim_identity_email mim-control-plane)" ]] || mim_fail "Cloud Run service must use the dedicated control-plane identity"

  MIN_SCALE=$(gcloud_capture \
    "Unable to inspect the control-plane minimum instance setting" \
    run services describe "$MIM_CONTROL_PLANE_SERVICE" \
    --region="$MIM_FIXED_REGION" \
    '--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/minScale)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$MIN_SCALE" == "0" ]] || mim_fail "Cloud Run service minimum instances must be 0"

  MAX_SCALE=$(gcloud_capture \
    "Unable to inspect the control-plane maximum instance setting" \
    run services describe "$MIM_CONTROL_PLANE_SERVICE" \
    --region="$MIM_FIXED_REGION" \
    '--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/maxScale)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$MAX_SCALE" == "1" ]] || mim_fail "Cloud Run service maximum instances must be 1"
fi
rm -f -- "$RUN_STATE_FILE"

ARTIFACT_STATE_FILE=$(mktemp)
ARTIFACT_STATE=$(gcloud_optional_state \
  "Unable to inspect Artifact Registry repository" \
  "$ARTIFACT_STATE_FILE" \
  artifacts repositories describe "$MIM_ARTIFACT_REPOSITORY" \
  --location="$MIM_FIXED_REGION" \
  '--format=value(name)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
if [[ "$ARTIFACT_STATE" == "exists" ]]; then
  ARTIFACT_FORMAT=$(gcloud_capture \
    "Unable to inspect Artifact Registry repository format" \
    artifacts repositories describe "$MIM_ARTIFACT_REPOSITORY" \
    --location="$MIM_FIXED_REGION" \
    '--format=value(format)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$ARTIFACT_FORMAT" == "DOCKER" || "$ARTIFACT_FORMAT" == "docker" ]] || mim_fail "Artifact Registry repository must use docker format"
fi
rm -f -- "$ARTIFACT_STATE_FILE"

FIRESTORE_STATE_FILE=$(mktemp)
FIRESTORE_STATE=$(gcloud_optional_state \
  "Unable to inspect Firestore database" \
  "$FIRESTORE_STATE_FILE" \
  firestore databases describe "$MIM_FIRESTORE_DATABASE" \
  '--format=value(name)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
if [[ "$FIRESTORE_STATE" == "exists" ]]; then
  FIRESTORE_LOCATION=$(gcloud_capture \
    "Unable to inspect Firestore location" \
    firestore databases describe "$MIM_FIRESTORE_DATABASE" \
    '--format=value(locationId)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$FIRESTORE_LOCATION" == "$MIM_FIXED_REGION" ]] || mim_fail "Firestore database location must stay in asia-northeast3"

  FIRESTORE_TYPE=$(gcloud_capture \
    "Unable to inspect Firestore database type" \
    firestore databases describe "$MIM_FIRESTORE_DATABASE" \
    '--format=value(type)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$FIRESTORE_TYPE" == "$MIM_FIRESTORE_TYPE" ]] || mim_fail "Firestore database must use Firestore Native mode"
fi
rm -f -- "$FIRESTORE_STATE_FILE"

QUEUE_STATE_FILE=$(mktemp)
TASKS_QUEUE_STATE=$(gcloud_optional_state \
  "Unable to inspect private worker queue" \
  "$QUEUE_STATE_FILE" \
  tasks queues describe "$MIM_PRIVATE_QUEUE" \
  --location="$MIM_FIXED_REGION" \
  '--format=value(name)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
if [[ "$TASKS_QUEUE_STATE" == "exists" ]]; then
  QUEUE_STATE=$(gcloud_capture \
    "Unable to inspect private worker queue state" \
    tasks queues describe "$MIM_PRIVATE_QUEUE" \
    --location="$MIM_FIXED_REGION" \
    '--format=value(state)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$QUEUE_STATE" == "$MIM_QUEUE_STATE" ]] || mim_fail "Private worker queue must remain RUNNING"
fi
rm -f -- "$QUEUE_STATE_FILE"

printf 'Preflight checks passed.\n'
