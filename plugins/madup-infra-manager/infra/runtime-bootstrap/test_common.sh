#!/usr/bin/env bash
set -euo pipefail

readonly RTB_OPERATOR_EMAIL='operator.test@madup.com'
readonly RTB_PERSONAL_EMAIL='person@example.com'
readonly RTB_PROJECT_ID='mim-prod-123456'
readonly RTB_PROJECT_NUMBER='123456789012'
readonly RTB_ORGANIZATION_ID='123456789012'
readonly RTB_BILLING_ACCOUNT_ID='ABCDEF-123456-7890AB'
readonly RTB_CLOUDFLARE_ISSUER='https://madup.cloudflareaccess.com'
readonly RTB_CLOUDFLARE_AUDIENCE='cf-aud-1234567890'
readonly RTB_APP_CLOUDFLARE_AUDIENCE='cf-app-aud-1234567890'
readonly RTB_PUBLIC_HOST_SUFFIX='madup.app'
readonly RTB_REGION='asia-northeast3'
readonly RTB_DIRECTORY_GROUP='mim-users@madup.com'
readonly RTB_DIRECTORY_ADMIN='directory.admin@madup.com'
readonly RTB_DIRECTORY_SA='mim-identity-sync@mim-prod-123456.iam.gserviceaccount.com'
readonly RTB_EDGE_SECRET='projects/mim-prod-123456/secrets/mim-edge-origin-v1/versions/1'
readonly RTB_APP_EDGE_SECRET='projects/mim-prod-123456/secrets/mim-app-gateway-origin-v1/versions/5'
readonly RTB_APP_EDGE_PREVIOUS_SECRET='projects/mim-prod-123456/secrets/mim-app-gateway-origin-v0/versions/4'
readonly RTB_SIGNING_SECRET='projects/mim-prod-123456/secrets/mim-desired-state-signing/versions/2'
readonly RTB_WEBHOOK_SECRET='projects/mim-prod-123456/secrets/mim-github-webhook/versions/3'
readonly RTB_APP_KEY_SECRET='projects/mim-prod-123456/secrets/mim-github-app-key/versions/4'
readonly RTB_RELEASE_SA='mim-release@mim-prod-123456.iam.gserviceaccount.com'
readonly RTB_APP_GATEWAY_SA='mim-app-gateway@mim-prod-123456.iam.gserviceaccount.com'
readonly RTB_BOOTSTRAP_SECRET='projects/mim-prod-123456/secrets/mim-runtime-bootstrap'
readonly RTB_BOOTSTRAP_VERSION_7='projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7'
readonly RTB_BOOTSTRAP_VERSION_8='projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/8'

rtb_write_private_file() {
  local path=$1
  local body=$2
  printf '%s' "$body" >"$path"
  chmod 600 "$path"
}

rtb_valid_bootstrap_body() {
  cat <<'EOF'
{
  "schema_version": 1,
  "project_id": "mim-prod-123456",
  "project_number": "123456789012",
  "organization_id": "123456789012",
  "billing_account_id": "ABCDEF-123456-7890AB",
  "operator_email": "operator.test@madup.com",
  "cloudflare_issuer": "https://madup.cloudflareaccess.com",
  "cloudflare_audience": "cf-aud-1234567890",
  "app_cloudflare_issuer": "https://madup.cloudflareaccess.com",
  "app_cloudflare_audience": "cf-app-aud-1234567890",
  "public_host_suffix": "madup.app",
  "region": "asia-northeast3",
  "directory_required_group_email": "mim-users@madup.com",
  "admin_members": [
    "group:mim-admins@madup.com",
    "user:operator.test@madup.com"
  ],
  "breakglass_members": [],
  "directory": {
    "admin_subject": "directory.admin@madup.com",
    "service_account_email": "mim-identity-sync@mim-prod-123456.iam.gserviceaccount.com"
  },
  "slack": {
    "required_scopes": [
      "chat:write",
      "commands"
    ]
  },
  "origin_hmac_keys": [
    {
      "key_id": "edge-current",
      "secret_version": "projects/mim-prod-123456/secrets/mim-edge-origin-v1/versions/1"
    }
  ],
  "app_origin_hmac_keys": [
    {
      "key_id": "app-current",
      "secret_version": "projects/mim-prod-123456/secrets/mim-app-gateway-origin-v1/versions/5"
    },
    {
      "key_id": "app-previous",
      "secret_version": "projects/mim-prod-123456/secrets/mim-app-gateway-origin-v0/versions/4"
    }
  ],
  "desired_state_signing_key_id": "deploy-key-202608",
  "desired_state_signing_secret_version": "projects/mim-prod-123456/secrets/mim-desired-state-signing/versions/2",
  "github_webhook_secret_version": "projects/mim-prod-123456/secrets/mim-github-webhook/versions/3",
  "github_app": {
    "app_id": "123456",
    "private_key_secret_version": "projects/mim-prod-123456/secrets/mim-github-app-key/versions/4",
    "installation_id": 303,
    "allowed_repository_ids": [
      101
    ],
    "bindings": [
      {
        "repository_numeric_id": 101,
        "owner": "madupmarketing",
        "name": "sample-app",
        "installation_id": 303,
        "repository_resource": "projects/mim-prod-123456/locations/asia-northeast3/connections/mim-github/repositories/sample-app"
      }
    ]
  },
  "build": {
    "builder_image": "asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-platform/mim-builder@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "build_service_account": "projects/mim-prod-123456/serviceAccounts/mim-build@mim-prod-123456.iam.gserviceaccount.com"
  },
  "deploy_worker": {
    "url": "https://mim-deploy-worker-123456789012.asia-northeast3.run.app/internal/deploy",
    "audience": "https://mim-deploy-worker-123456789012.asia-northeast3.run.app",
    "service_account_email": "mim-deploy-worker@mim-prod-123456.iam.gserviceaccount.com"
  },
  "app_gateway": {
    "url": "https://mim-app-gateway-123456789012.asia-northeast3.run.app",
    "audience": "https://mim-app-gateway-123456789012.asia-northeast3.run.app",
    "service_account_email": "mim-app-gateway@mim-prod-123456.iam.gserviceaccount.com"
  },
  "app_authorization": {
    "url": "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app/v1/apps/authorize",
    "audience": "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app",
    "service_account_email": "mim-schedule-gateway@mim-prod-123456.iam.gserviceaccount.com"
  },
  "schedule_gateway": {
    "url": "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app/v1/schedules/execute",
    "audience": "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app",
    "service_account_email": "mim-schedule-gateway@mim-prod-123456.iam.gserviceaccount.com"
  }
}
EOF
}

rtb_write_valid_input() {
  local path=$1
  rtb_write_private_file "$path" "$(rtb_valid_bootstrap_body)"
}

rtb_sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    LC_ALL=C shasum -a 256 "$1" | awk '{print $1}'
    return
  fi
  sha256sum "$1" | awk '{print $1}'
}

rtb_assert_contains() {
  local file=$1
  local expected=$2
  local case_name=$3
  if ! grep -Fq -- "$expected" "$file"; then
    printf 'FAIL %s: missing %s\n' "$case_name" "$expected" >&2
    cat "$file" >&2 || true
    return 1
  fi
}
rtb_assert_not_contains() {
  local file=$1
  local unexpected=$2
  local case_name=$3
  if grep -Fq -- "$unexpected" "$file"; then
    printf 'FAIL %s: unexpected %s\n' "$case_name" "$unexpected" >&2
    cat "$file" >&2 || true
    return 1
  fi
}
