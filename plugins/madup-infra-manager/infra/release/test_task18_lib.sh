#!/usr/bin/env bash
set -euo pipefail

TASK18_OPERATOR_EMAIL=operator.test@madup.com
TASK18_PROJECT_ID=mim-prod-123456
TASK18_ORGANIZATION_ID=123456789012
TASK18_BILLING_ACCOUNT_ID=ABCDEF-123456-7890AB
TASK18_CLOUDFLARE_ACCOUNT_ID=cf-account-123456
TASK18_CLOUDFLARE_ZONE_ID=deadbeefcafefeed
TASK18_CLOUDFLARE_TEAM_NAME=madup-marketing
TASK18_GITHUB_REPOSITORY_IDS=111111111,222222222
TASK18_SLACK_APP_ID=A123456789
TASK18_SLACK_APPROVED_ORG_ID=E123456789
TASK18_SLACK_APPROVED_WORKSPACE_IDS=T123456789,T987654321
TASK18_SLACK_REDIRECT_URI=https://mim.madup.app/slack/oauth/callback
TASK18_SLACK_REQUIRED_SCOPES=chat:write,commands
TASK18_SLACK_CONFIG_TOKEN='xoxe.xoxp''-task18-config-token'
TASK18_GITHUB_WEBHOOK_SECRET=task18-github-webhook-secret-value-0123456789
TASK18_SOURCE_COMMIT=0123456789abcdef0123456789abcdef01234567
TASK18_REVIEWED_BUILDER_BUILD_ID=builder-build-123456
TASK18_REVIEWED_APP_GATEWAY_BUILD_ID=app-gateway-build-222333
TASK18_REVIEWED_RUNTIME_BUILD_ID=runtime-build-654321
TASK18_REVIEWED_BUILDER_IMAGE_URI=asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-platform/mim-builder@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI=asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-platform/app-gateway@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
TASK18_REVIEWED_RUNTIME_IMAGE_URI=asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-control-plane/runtime@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION=projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7
TASK18_APP_GATEWAY_PROOF_SECRET_VERSION=projects/mim-prod-123456/secrets/mim-app-gateway-origin-v1/versions/11
TASK18_APP_CLOUDFLARE_ACCESS_ISSUER=https://madup-marketing.cloudflareaccess.com
TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE=cf-app-audience-12345678
TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID=app-current
TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID=app-previous
TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION=projects/mim-prod-123456/secrets/mim-app-gateway-origin-v0/versions/10
TASK18_TENANT_EVIDENCE_VERSION=mim-slack-tenant-evidence-v1
TASK18_TENANT_EVIDENCE_GENERATED_AT=1785799800
TASK18_PRIVATE_PROTECTED_1=sensitive-prod-12345
TASK18_PRIVATE_PROTECTED_2=reserved-prod-67890

task18_write_private_file() {
  local path=$1
  local body=$2
  printf '%s' "$body" >"$path"
  chmod 600 "$path"
}

task18_write_sha_sidecar() {
  local path=$1
  printf '%s  %s\n' "$(LC_ALL=C shasum -a 256 "$path" | awk '{print $1}')" "$(basename "$path")" >"$path.sha256"
  chmod 600 "$path.sha256"
}

task18_write_valid_config() {
  local path=$1
  task18_write_private_file "$path" "$(cat <<EOF
MIM_OPERATOR_EMAIL=$TASK18_OPERATOR_EMAIL
MIM_PROJECT_ID=$TASK18_PROJECT_ID
MIM_ORGANIZATION_ID=$TASK18_ORGANIZATION_ID
MIM_BILLING_ACCOUNT_ID=$TASK18_BILLING_ACCOUNT_ID
MIM_CLOUDFLARE_ACCOUNT_ID=$TASK18_CLOUDFLARE_ACCOUNT_ID
MIM_CLOUDFLARE_ZONE_ID=$TASK18_CLOUDFLARE_ZONE_ID
MIM_CLOUDFLARE_TEAM_NAME=$TASK18_CLOUDFLARE_TEAM_NAME
MIM_GITHUB_REPOSITORY_IDS=$TASK18_GITHUB_REPOSITORY_IDS
MIM_SLACK_ENABLED=true
MIM_SLACK_APP_ID=$TASK18_SLACK_APP_ID
MIM_SLACK_APPROVED_ORG_ID=$TASK18_SLACK_APPROVED_ORG_ID
MIM_SLACK_APPROVED_WORKSPACE_IDS=$TASK18_SLACK_APPROVED_WORKSPACE_IDS
EOF
)"
}

task18_write_google_only_config() {
  local path=$1
  task18_write_private_file "$path" "$(cat <<EOF
MIM_OPERATOR_EMAIL=$TASK18_OPERATOR_EMAIL
MIM_PROJECT_ID=$TASK18_PROJECT_ID
MIM_ORGANIZATION_ID=$TASK18_ORGANIZATION_ID
MIM_BILLING_ACCOUNT_ID=$TASK18_BILLING_ACCOUNT_ID
MIM_CLOUDFLARE_ACCOUNT_ID=$TASK18_CLOUDFLARE_ACCOUNT_ID
MIM_CLOUDFLARE_ZONE_ID=$TASK18_CLOUDFLARE_ZONE_ID
MIM_CLOUDFLARE_TEAM_NAME=$TASK18_CLOUDFLARE_TEAM_NAME
MIM_GITHUB_REPOSITORY_IDS=$TASK18_GITHUB_REPOSITORY_IDS
EOF
)"
}

task18_write_config_with_extra_line() {
  local path=$1
  local extra_line=$2
  task18_write_private_file "$path" "$(cat <<EOF
MIM_OPERATOR_EMAIL=$TASK18_OPERATOR_EMAIL
MIM_PROJECT_ID=$TASK18_PROJECT_ID
MIM_ORGANIZATION_ID=$TASK18_ORGANIZATION_ID
MIM_BILLING_ACCOUNT_ID=$TASK18_BILLING_ACCOUNT_ID
MIM_CLOUDFLARE_ACCOUNT_ID=$TASK18_CLOUDFLARE_ACCOUNT_ID
MIM_CLOUDFLARE_ZONE_ID=$TASK18_CLOUDFLARE_ZONE_ID
MIM_CLOUDFLARE_TEAM_NAME=$TASK18_CLOUDFLARE_TEAM_NAME
MIM_GITHUB_REPOSITORY_IDS=$TASK18_GITHUB_REPOSITORY_IDS
MIM_SLACK_ENABLED=true
MIM_SLACK_APP_ID=$TASK18_SLACK_APP_ID
MIM_SLACK_APPROVED_ORG_ID=$TASK18_SLACK_APPROVED_ORG_ID
MIM_SLACK_APPROVED_WORKSPACE_IDS=$TASK18_SLACK_APPROVED_WORKSPACE_IDS
$extra_line
EOF
)"
}

task18_write_protected_file() {
  local path=$1
  local body=${2:-$TASK18_PRIVATE_PROTECTED_1$'\n'$TASK18_PRIVATE_PROTECTED_2$'\n'}
  task18_write_private_file "$path" "$body"
}

task18_assert_contains() {
  local file=$1
  local expected=$2
  local case_name=$3
  if ! grep -Fq -- "$expected" "$file"; then
    printf 'FAIL %s: missing %s\n' "$case_name" "$expected" >&2
    cat "$file" >&2 || true
    return 1
  fi
}

task18_assert_not_contains() {
  local file=$1
  local unexpected=$2
  local case_name=$3
  if grep -Fq -- "$unexpected" "$file"; then
    printf 'FAIL %s: unexpected %s\n' "$case_name" "$unexpected" >&2
    cat "$file" >&2 || true
    return 1
  fi
}

task18_assert_body_not_leaked() {
  local file=$1
  local body=$2
  local case_name=$3
  local line
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    task18_assert_not_contains "$file" "$line" "$case_name" || return 1
  done <<<"$body"
}

task18_write_tenant_evidence() {
  local path=$1
  local generated_at=${2:-$TASK18_TENANT_EVIDENCE_GENERATED_AT}
  task18_write_private_file "$path" "$(cat <<EOF
{
  "version": "$TASK18_TENANT_EVIDENCE_VERSION",
  "generated_at_epoch": $generated_at,
  "app_id": "$TASK18_SLACK_APP_ID",
  "approved_org_id": "$TASK18_SLACK_APPROVED_ORG_ID",
  "approved_workspace_ids": ["T123456789", "T987654321"]
}
EOF
)"
  task18_write_sha_sidecar "$path"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  . "$SCRIPT_DIR/task18_lib.sh"

  TMP_DIR=$(mktemp -d)
  FAILURES=0
  trap 'rm -rf "$TMP_DIR"' EXIT

  task18_write_google_only_config "$TMP_DIR/google-only.env"
  if ! mim_task18_load_config "$TMP_DIR/google-only.env"; then
    printf 'FAIL load_google_only_config: expected config to load\n' >&2
    FAILURES=$((FAILURES + 1))
  else
    [[ "${MIM_SLACK_ENABLED:-}" == "false" ]] || {
      printf 'FAIL load_google_only_config: expected MIM_SLACK_ENABLED=false got %s\n' "${MIM_SLACK_ENABLED:-<unset>}" >&2
      FAILURES=$((FAILURES + 1))
    }
    [[ -z "${MIM_SLACK_APP_ID:-}" ]] || {
      printf 'FAIL load_google_only_config: expected empty MIM_SLACK_APP_ID\n' >&2
      FAILURES=$((FAILURES + 1))
    }
  fi

  task18_write_private_file "$TMP_DIR/invalid-slack-enabled.env" "$(cat <<EOF
MIM_OPERATOR_EMAIL=$TASK18_OPERATOR_EMAIL
MIM_PROJECT_ID=$TASK18_PROJECT_ID
MIM_ORGANIZATION_ID=$TASK18_ORGANIZATION_ID
MIM_BILLING_ACCOUNT_ID=$TASK18_BILLING_ACCOUNT_ID
MIM_CLOUDFLARE_ACCOUNT_ID=$TASK18_CLOUDFLARE_ACCOUNT_ID
MIM_CLOUDFLARE_ZONE_ID=$TASK18_CLOUDFLARE_ZONE_ID
MIM_CLOUDFLARE_TEAM_NAME=$TASK18_CLOUDFLARE_TEAM_NAME
MIM_GITHUB_REPOSITORY_IDS=$TASK18_GITHUB_REPOSITORY_IDS
MIM_SLACK_ENABLED=TRUE
EOF
)"
  if (mim_task18_load_config "$TMP_DIR/invalid-slack-enabled.env") >"$TMP_DIR/invalid.out" 2>&1; then
    printf 'FAIL invalid_slack_enabled: expected failure\n' >&2
    FAILURES=$((FAILURES + 1))
  elif ! grep -Fq "Invalid MIM_SLACK_ENABLED" "$TMP_DIR/invalid.out"; then
    printf 'FAIL invalid_slack_enabled: missing validation message\n' >&2
    cat "$TMP_DIR/invalid.out" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi

  if [[ "$FAILURES" -ne 0 ]]; then
    printf 'FAIL: %s task18 lib assertions failed\n' "$FAILURES" >&2
    exit 1
  fi
  printf 'PASS test_task18_lib.sh\n'
fi
