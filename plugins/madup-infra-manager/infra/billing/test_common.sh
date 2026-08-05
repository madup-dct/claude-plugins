#!/usr/bin/env bash
set -euo pipefail

readonly BILLING_OPERATOR_EMAIL='operator.test@madup.com'
readonly BILLING_PROJECT_ID='mim-prod-123456'
readonly BILLING_ORGANIZATION_ID='123456789012'
readonly BILLING_BILLING_ACCOUNT_ID='ABCDEF-123456-7890AB'
readonly BILLING_CLOUDFLARE_ACCOUNT_ID='cf-account-123'
readonly BILLING_CLOUDFLARE_ZONE_ID='cfzone12345678'
readonly BILLING_CLOUDFLARE_TEAM_NAME='madup'
readonly BILLING_GITHUB_REPOSITORY_IDS='101'
readonly BILLING_SLACK_APP_ID='A1234567'
readonly BILLING_SLACK_APPROVED_ORG_ID='E123456789'
readonly BILLING_SLACK_APPROVED_WORKSPACE_IDS='T123456789'
readonly BILLING_MAINTENANCE_EMAIL='mim-maintenance@mim-prod-123456.iam.gserviceaccount.com'
readonly BILLING_RAW_DATASET='mim_billing_export'
readonly BILLING_RAW_PREFIX='gcp_billing_export_resource_v1_'
readonly BILLING_SECURE_DATASET='mim_billing_secure'
readonly BILLING_SECURE_VIEW='mim_usage_costs_v1'

billing_write_private_file() {
  local path=$1
  local body=$2
  printf '%s' "$body" >"$path"
  chmod 600 "$path"
}

billing_write_config() {
  local path=$1
  billing_write_private_file "$path" "$(cat <<'EOF'
MIM_OPERATOR_EMAIL=operator.test@madup.com
MIM_PROJECT_ID=mim-prod-123456
MIM_ORGANIZATION_ID=123456789012
MIM_BILLING_ACCOUNT_ID=ABCDEF-123456-7890AB
MIM_CLOUDFLARE_ACCOUNT_ID=cf-account-123
MIM_CLOUDFLARE_ZONE_ID=cfzone12345678
MIM_CLOUDFLARE_TEAM_NAME=madup
MIM_GITHUB_REPOSITORY_IDS=101
MIM_SLACK_APP_ID=A1234567
MIM_SLACK_APPROVED_ORG_ID=E123456789
MIM_SLACK_APPROVED_WORKSPACE_IDS=T123456789
EOF
)"
}

billing_assert_contains() {
  local file=$1
  local expected=$2
  local case_name=$3
  if ! grep -Fq -- "$expected" "$file"; then
    printf 'FAIL %s: missing %s\n' "$case_name" "$expected" >&2
    cat "$file" >&2 || true
    return 1
  fi
}

billing_assert_not_contains() {
  local file=$1
  local unexpected=$2
  local case_name=$3
  if grep -Fq -- "$unexpected" "$file"; then
    printf 'FAIL %s: unexpected %s\n' "$case_name" "$unexpected" >&2
    cat "$file" >&2 || true
    return 1
  fi
}
