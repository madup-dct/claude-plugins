#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG_FILE="${MIM_CONFIG_FILE:-$SCRIPT_DIR/config.env}"
PROTECTED_PROJECTS_FILE="${MIM_PROTECTED_PROJECTS_FILE:-$SCRIPT_DIR/protected-projects.exact}"

# shellcheck source=config_lib.sh
. "$SCRIPT_DIR/config_lib.sh"

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

gcloud_capture() {
  local description=$1
  shift

  local output
  if ! output=$(gcloud "$@" 2>/dev/null); then
    fail "$description"
  fi

  printf '%s' "$output"
}

mim_load_config "$CONFIG_FILE"
mim_assert_project_not_protected "$MIM_PROJECT_ID" "$PROTECTED_PROJECTS_FILE"

readonly MIM_IAP_MEMBER="$(mim_derive_iap_member)"
: "$MIM_FIXED_APEX_ACTION"
: "$MIM_FIXED_HOSTNAME"
: "$MIM_IAP_MEMBER"

command -v gcloud >/dev/null 2>&1 || fail "gcloud CLI is required"

ACTIVE_ACCOUNT=$(
  gcloud_capture \
    "Unable to determine the active gcloud account" \
    auth list \
    --filter=status:ACTIVE \
    '--format=value(account)' \
    --account="$MIM_OPERATOR_EMAIL"
)
[[ "$ACTIVE_ACCOUNT" == "$MIM_OPERATOR_EMAIL" ]] || fail "Active gcloud account does not match the configured operator"

PROJECT_ID=$(
  gcloud_capture \
    "Unable to describe the configured project" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(projectId)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"
)
[[ "$PROJECT_ID" == "$MIM_PROJECT_ID" ]] || fail "Configured project mismatch"

PROJECT_PARENT_TYPE=$(
  gcloud_capture \
    "Unable to determine the project parent type" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(parent.type)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"
)
[[ "$PROJECT_PARENT_TYPE" == "organization" ]] || fail "Project parent must be an organization"

PROJECT_PARENT_ID=$(
  gcloud_capture \
    "Unable to determine the project organization" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(parent.id)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"
)
[[ "$PROJECT_PARENT_ID" == "$MIM_ORGANIZATION_ID" ]] || fail "Project organization mismatch"

BILLING_ENABLED=$(
  gcloud_capture \
    "Unable to determine project billing status" \
    billing projects describe "$MIM_PROJECT_ID" \
    '--format=value(billingEnabled)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"
)
[[ "$BILLING_ENABLED" == "True" ]] || fail "Billing must be linked"

BILLING_ACCOUNT_NAME=$(
  gcloud_capture \
    "Unable to determine the billing account" \
    billing projects describe "$MIM_PROJECT_ID" \
    '--format=value(billingAccountName)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"
)
[[ "$BILLING_ACCOUNT_NAME" == "billingAccounts/$MIM_BILLING_ACCOUNT_ID" ]] || fail "Billing account mismatch"

printf 'Preflight checks passed.\n'
