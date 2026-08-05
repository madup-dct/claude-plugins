#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STATE_DIR="$SCRIPT_DIR/.state"
APPLY_SCRIPT="$SCRIPT_DIR/apply.sh"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"; rm -f "$STATE_DIR"/task17-*.json "$STATE_DIR"/task17-*.json.sha256 "$STATE_DIR"/task17-*.link 2>/dev/null || true' EXIT
mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR"/task17-*.json "$STATE_DIR"/task17-*.json.sha256 "$STATE_DIR"/task17-*.link 2>/dev/null || true

OPERATOR_EMAIL=operator.test@madup.com
PROJECT_ID=mim-prod-123456
ORGANIZATION_ID=123456789012
BILLING_ACCOUNT_ID=ABCDEF-123456-7890AB
PROJECT_NUMBER=987654321012
CLOUDFLARE_ACCOUNT_ID=cf-account-123456
CLOUDFLARE_ZONE_ID=deadbeefcafefeed
CLOUDFLARE_TEAM_NAME=madup-marketing
GITHUB_REPOSITORY_IDS=123456789,987654321
SLACK_APP_ID=A123456789
SLACK_APPROVED_ORG_ID=E123456789
SLACK_APPROVED_WORKSPACE_IDS=T123456789,T987654321
CONTROL_PLANE_EMAIL="mim-control-plane@$PROJECT_ID.iam.gserviceaccount.com"
APP_GATEWAY_EMAIL="mim-app-gateway@$PROJECT_ID.iam.gserviceaccount.com"
DEPLOY_WORKER_EMAIL="mim-deploy-worker@$PROJECT_ID.iam.gserviceaccount.com"
BUILD_EMAIL="mim-build@$PROJECT_ID.iam.gserviceaccount.com"
SCHEDULE_GATEWAY_EMAIL="mim-schedule-gateway@$PROJECT_ID.iam.gserviceaccount.com"
MAINTENANCE_EMAIL="mim-maintenance@$PROJECT_ID.iam.gserviceaccount.com"
IDENTITY_SYNC_EMAIL="mim-identity-sync@$PROJECT_ID.iam.gserviceaccount.com"
RELEASE_EMAIL="mim-release@$PROJECT_ID.iam.gserviceaccount.com"
PRIVATE_PROTECTED_A="sensitive-prod-12345"
PRIVATE_PROTECTED_B="reserved-prod-67890"
PRIVATE_PROTECTED_C="quarantine-prod-24680"

STUB_BIN="$TMP_DIR/bin"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"

case "$*" in
  auth\ list\ *"--format=value(account)"*)
    printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-}"
    ;;
  projects\ describe\ *"--format=value(projectId)"*)
    printf '%s\n' "${GCLOUD_PROJECT_ID:-}"
    ;;
  projects\ describe\ *"--format=value(parent.type)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_TYPE:-}"
    ;;
  projects\ describe\ *"--format=value(parent.id)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_ID:-}"
    ;;
  projects\ describe\ *"--format=value(projectNumber)"*)
    printf '%s\n' "${GCLOUD_PROJECT_NUMBER:-}"
    ;;
  billing\ projects\ describe\ *"--format=value(billingEnabled)"*)
    printf '%s\n' "${GCLOUD_BILLING_ENABLED:-}"
    ;;
  billing\ projects\ describe\ *"--format=value(billingAccountName)"*)
    printf '%s\n' "${GCLOUD_BILLING_ACCOUNT_NAME:-}"
    ;;
  services\ list\ *"--format=value(config.name)"*)
    printf '%s\n' "${GCLOUD_ENABLED_APIS:-}"
    ;;
  projects\ get-iam-policy\ *"--format=json"*)
    printf '%s\n' "${GCLOUD_PROJECT_IAM_POLICY_JSON:-{\"bindings\":[]}}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.role=roles/run.invoker"*)
    printf '%s\n' "${GCLOUD_PROJECT_RUN_INVOKERS:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-control-plane@"*)
    printf '%s\n' "${GCLOUD_CONTROL_PLANE_ROLES:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-app-gateway@"*)
    printf '%s\n' "${GCLOUD_APP_GATEWAY_ROLES:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-deploy-worker@"*)
    printf '%s\n' "${GCLOUD_DEPLOY_WORKER_ROLES:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-build@"*)
    printf '%s\n' "${GCLOUD_BUILD_ROLES:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-schedule-gateway@"*)
    printf '%s\n' "${GCLOUD_SCHEDULE_GATEWAY_ROLES:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-maintenance@"*)
    printf '%s\n' "${GCLOUD_MAINTENANCE_ROLES:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-identity-sync@"*)
    printf '%s\n' "${GCLOUD_IDENTITY_SYNC_ROLES:-}"
    ;;
  projects\ get-iam-policy\ *"--filter=bindings.members:serviceAccount:mim-release@"*)
    printf '%s\n' "${GCLOUD_RELEASE_ROLES:-}"
    ;;
  run\ services\ describe\ *"--format=value(metadata.name)"*)
    case "${GCLOUD_RUN_SERVICE_STATUS:-missing}" in
      missing)
        printf 'ERROR: (gcloud.run.services.describe) Cannot find service [mim-control-plane]\n' >&2
        exit 1
        ;;
      exists)
        printf 'mim-control-plane\n'
        ;;
    esac
    ;;
  run\ services\ describe\ *"--format=value(spec.template.spec.serviceAccountName)"*)
    printf '%s\n' "${GCLOUD_RUN_SERVICE_ACCOUNT:-$CONTROL_PLANE_EMAIL}"
    ;;
  run\ services\ describe\ *"--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/minScale)"*)
    printf '%s\n' "${GCLOUD_MIN_SCALE:-0}"
    ;;
  run\ services\ describe\ *"--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/maxScale)"*)
    printf '%s\n' "${GCLOUD_MAX_SCALE:-1}"
    ;;
  iam\ service-accounts\ describe\ *)
    case "${GCLOUD_SERVICE_ACCOUNTS_EXIST:-missing}" in
      missing)
        printf 'NOT_FOUND: service account missing\n' >&2
        exit 1
        ;;
      exists)
        printf '%s\n' "$4"
        ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-build@*"--format=json"*)
    case "${GCLOUD_BUILD_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_BUILD_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-control-plane@*"--format=json"*)
    case "${GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_CONTROL_PLANE_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-app-gateway@*"--format=json"*)
    case "${GCLOUD_APP_GATEWAY_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_APP_GATEWAY_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-deploy-worker@*"--format=json"*)
    case "${GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-identity-sync@*"--format=json"*)
    case "${GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-maintenance@*"--format=json"*)
    case "${GCLOUD_MAINTENANCE_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_MAINTENANCE_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-release@*"--format=json"*)
    case "${GCLOUD_RELEASE_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_RELEASE_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-schedule-gateway@*"--format=json"*)
    case "${GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: service account %s missing\n' "$4" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  iam\ service-accounts\ create\ *)
    :
    ;;
  iam\ service-accounts\ add-iam-policy-binding\ *)
    :
    ;;
  artifacts\ repositories\ describe\ *"--format=value(name)"*)
    case "${GCLOUD_ARTIFACT_REPOSITORY_STATUS:-missing}" in
      missing)
        printf 'NOT_FOUND: repository missing\n' >&2
        exit 1
        ;;
      exists)
        printf 'projects/%s/locations/asia-northeast3/repositories/mim-control-plane\n' "${GCLOUD_PROJECT_ID:-}"
        ;;
    esac
    ;;
  artifacts\ repositories\ describe\ *"--format=value(format)"*)
    printf '%s\n' "${GCLOUD_ARTIFACT_REPOSITORY_FORMAT:-DOCKER}"
    ;;
  artifacts\ repositories\ get-iam-policy\ mim\ *"--format=json"*)
    case "${GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: repository mim missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  artifacts\ repositories\ create\ *)
    :
    ;;
  artifacts\ repositories\ add-iam-policy-binding\ *)
    :
    ;;
  firestore\ databases\ describe\ *"--format=value(name)"*)
    case "${GCLOUD_FIRESTORE_STATUS:-missing}" in
      missing)
        printf 'NOT_FOUND: database missing\n' >&2
        exit 1
        ;;
      exists)
        printf 'projects/%s/databases/(default)\n' "${GCLOUD_PROJECT_ID:-}"
        ;;
    esac
    ;;
  firestore\ databases\ describe\ *"--format=value(locationId)"*)
    printf '%s\n' "${GCLOUD_FIRESTORE_LOCATION:-asia-northeast3}"
    ;;
  firestore\ databases\ describe\ *"--format=value(type)"*)
    printf '%s\n' "${GCLOUD_FIRESTORE_TYPE:-FIRESTORE_NATIVE}"
    ;;
  firestore\ databases\ create\ *)
    :
    ;;
  firestore\ indexes\ composite\ list\ *"--format=json"*)
    case "${GCLOUD_INDEX_LIST_STATUS:-ok}" in
      ok)
        if [[ -n "${GCLOUD_INDEX_STATE_FILE:-}" && -f "${GCLOUD_INDEX_STATE_FILE:-}" ]]; then
          cat "$GCLOUD_INDEX_STATE_FILE"
        else
          printf '%s\n' "${GCLOUD_INDEX_LIST_JSON:-[]}"
        fi
        ;;
      error)
        printf 'permission denied\n' >&2
        exit 1
        ;;
      *)
        printf 'unexpected composite index list status: %s\n' "${GCLOUD_INDEX_LIST_STATUS:-}" >&2
        exit 99
        ;;
    esac
    ;;
  firestore\ indexes\ composite\ create\ *"--collection-group=operations"*"--query-scope=collection"*)
    case "${GCLOUD_INDEX_CREATE_STATUS:-ok}" in
      ok)
        if [[ -n "${GCLOUD_INDEX_STATE_FILE:-}" && -n "${GCLOUD_INDEX_POST_CREATE_JSON:-}" ]]; then
          printf '%s' "$GCLOUD_INDEX_POST_CREATE_JSON" >"$GCLOUD_INDEX_STATE_FILE"
        fi
        ;;
      error)
        printf 'permission denied\n' >&2
        exit 1
        ;;
      *)
        printf 'unexpected composite index create status: %s\n' "${GCLOUD_INDEX_CREATE_STATUS:-}" >&2
        exit 99
        ;;
    esac
    ;;
  firestore\ fields\ ttls\ list\ *"--collection-group=origin_request_claims"*"--format=json"*)
    case "${GCLOUD_TTL_LIST_STATUS:-ok}" in
      ok)
        if [[ -n "${GCLOUD_TTL_STATE_FILE:-}" && -f "${GCLOUD_TTL_STATE_FILE:-}" ]]; then
          cat "$GCLOUD_TTL_STATE_FILE"
        else
          printf '%s\n' "${GCLOUD_TTL_LIST_JSON:-[]}"
        fi
        ;;
      error)
        printf 'permission denied\n' >&2
        exit 1
        ;;
      *)
        printf 'unexpected ttl list status: %s\n' "${GCLOUD_TTL_LIST_STATUS:-}" >&2
        exit 99
        ;;
    esac
    ;;
  firestore\ fields\ ttls\ update\ expires_at\ *"--collection-group=origin_request_claims"*"--enable-ttl"*"--async"*)
    case "${GCLOUD_TTL_UPDATE_STATUS:-ok}" in
      ok)
        if [[ -n "${GCLOUD_TTL_STATE_FILE:-}" && -n "${GCLOUD_TTL_POST_UPDATE_JSON:-}" ]]; then
          printf '%s' "$GCLOUD_TTL_POST_UPDATE_JSON" >"$GCLOUD_TTL_STATE_FILE"
        fi
        ;;
      error)
        printf 'permission denied\n' >&2
        exit 1
        ;;
      *)
        printf 'unexpected ttl update status: %s\n' "${GCLOUD_TTL_UPDATE_STATUS:-}" >&2
        exit 99
        ;;
    esac
    ;;
  tasks\ queues\ describe\ *"--format=value(name)"*)
    case "${GCLOUD_QUEUE_STATUS:-missing}" in
      missing)
        printf 'NOT_FOUND: queue missing\n' >&2
        exit 1
        ;;
      exists)
        printf 'projects/%s/locations/asia-northeast3/queues/mim-private-workers\n' "${GCLOUD_PROJECT_ID:-}"
        ;;
    esac
    ;;
  tasks\ queues\ describe\ *"--format=value(state)"*)
    printf '%s\n' "${GCLOUD_QUEUE_STATE:-RUNNING}"
    ;;
  tasks\ queues\ describe\ *"--format=value(retryConfig.maxAttempts)"*)
    printf '%s\n' "${GCLOUD_QUEUE_MAX_ATTEMPTS:-4}"
    ;;
  tasks\ queues\ describe\ *"--format=value(retryConfig.maxRetryDuration)"*)
    printf '%s\n' "${GCLOUD_QUEUE_MAX_RETRY_DURATION:-300s}"
    ;;
  tasks\ queues\ describe\ *"--format=value(retryConfig.minBackoff)"*)
    printf '%s\n' "${GCLOUD_QUEUE_MIN_BACKOFF:-5s}"
    ;;
  tasks\ queues\ describe\ *"--format=value(retryConfig.maxBackoff)"*)
    printf '%s\n' "${GCLOUD_QUEUE_MAX_BACKOFF:-60s}"
    ;;
  tasks\ queues\ describe\ *"--format=value(retryConfig.maxDoublings)"*)
    printf '%s\n' "${GCLOUD_QUEUE_MAX_DOUBLINGS:-3}"
    ;;
  tasks\ queues\ create\ *)
    :
    ;;
  secrets\ describe\ *"--format=value(name)"*)
    case "${GCLOUD_SECRET_STATUS:-missing}" in
      missing)
        printf 'NOT_FOUND: secret missing\n' >&2
        exit 1
        ;;
      exists)
        printf 'projects/%s/secrets/%s\n' "${GCLOUD_PROJECT_ID:-}" "${4:-secret}"
        ;;
    esac
    ;;
  secrets\ get-iam-policy\ mim-runtime-bootstrap\ *"--format=json"*)
    case "${GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_BOOTSTRAP_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-runtime-bootstrap missing\n' >&2; exit 1 ;;
      misleading_not_found) printf 'NOT_FOUND: host not found while inspecting secret transport\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  secrets\ get-iam-policy\ mim-edge-origin-v1\ *"--format=json"*)
    case "${GCLOUD_EDGE_ORIGIN_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-edge-origin-v1 missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  secrets\ get-iam-policy\ mim-app-gateway-origin-v0\ *"--format=json"*)
    case "${GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_STATUS:-missing}" in
      exists) printf '%s\n' "${GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-app-gateway-origin-v0 missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  secrets\ get-iam-policy\ mim-app-gateway-origin-v1\ *"--format=json"*)
    case "${GCLOUD_APP_GATEWAY_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_APP_GATEWAY_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-app-gateway-origin-v1 missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  secrets\ get-iam-policy\ mim-desired-state-signing\ *"--format=json"*)
    case "${GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-desired-state-signing missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  secrets\ get-iam-policy\ mim-github-webhook\ *"--format=json"*)
    case "${GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-github-webhook missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  secrets\ get-iam-policy\ mim-github-app-key\ *"--format=json"*)
    case "${GCLOUD_GITHUB_APP_KEY_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-github-app-key missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
    ;;
  secrets\ create\ *)
    :
    ;;
  secrets\ add-iam-policy-binding\ mim-runtime-bootstrap\ *)
    :
    ;;
  secrets\ add-iam-policy-binding\ mim-*\ *)
    :
    ;;
  projects\ add-iam-policy-binding\ *)
    :
    ;;
  services\ enable\ *)
    :
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/gcloud"

cat >"$STUB_BIN/bq" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf 'bq %s\n' "$*" >> "${GCLOUD_LOG:?}"

case "$*" in
  show\ --format=prettyjson\ *:mim_billing_export)
    case "${BQ_DATASET_STATUS:-exists}" in
      exists) printf '%s\n' "${BQ_DATASET_JSON:-{\"datasetReference\":{\"projectId\":\"'$PROJECT_ID'\",\"datasetId\":\"mim_billing_export\"},\"access\":[]}}" ;;
      missing) printf 'NOT_FOUND: dataset %s:mim_billing_export was not found\n' "${GCLOUD_PROJECT_ID:-}" >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
      misleading_not_found) printf 'host not found while contacting billing metadata service\n' >&2; exit 1 ;;
    esac
    ;;
  *)
    printf 'unexpected bq invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/bq"

FAILURES=0

write_private_file() {
  local path=$1
  local body=$2
  printf '%s' "$body" >"$path"
  chmod 600 "$path"
}

write_valid_config() {
  local path=$1
  write_private_file "$path" "$(cat <<EOF
MIM_OPERATOR_EMAIL=$OPERATOR_EMAIL
MIM_PROJECT_ID=$PROJECT_ID
MIM_ORGANIZATION_ID=$ORGANIZATION_ID
MIM_BILLING_ACCOUNT_ID=$BILLING_ACCOUNT_ID
MIM_CLOUDFLARE_ACCOUNT_ID=$CLOUDFLARE_ACCOUNT_ID
MIM_CLOUDFLARE_ZONE_ID=$CLOUDFLARE_ZONE_ID
MIM_CLOUDFLARE_TEAM_NAME=$CLOUDFLARE_TEAM_NAME
MIM_GITHUB_REPOSITORY_IDS=$GITHUB_REPOSITORY_IDS
MIM_SLACK_APP_ID=$SLACK_APP_ID
MIM_SLACK_APPROVED_ORG_ID=$SLACK_APPROVED_ORG_ID
MIM_SLACK_APPROVED_WORKSPACE_IDS=$SLACK_APPROVED_WORKSPACE_IDS
EOF
)"
}

write_protected_file() {
  local path=$1
  local body=${2:-other-project-12345$'\n'}
  write_private_file "$path" "$body"
}

readonly DEFAULT_MIM_SECRET_CONDITION='resource.name.startsWith("projects/mim-prod-123456/secrets/mim-sec-")'
readonly DEFAULT_MIM_RUN_CONDITION='resource.name.startsWith("projects/mim-prod-123456/locations/asia-northeast3/services/mim-svc-") || resource.name.startsWith("projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-job-")'
readonly DEFAULT_MIM_JOB_CONDITION='resource.name.startsWith("projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-job-")'
readonly DEFAULT_MIM_WORKLOAD_SA_CONDITION='resource.type == "iam.googleapis.com/ServiceAccount" && resource.name.startsWith("projects/mim-prod-123456/serviceAccounts/mim-wrk-")'
readonly DEFAULT_MIM_RELEASE_CONDITION='resource.name == "projects/mim-prod-123456/locations/asia-northeast3/services/mim-control-plane" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/services/mim-app-gateway" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/services/mim-deploy-worker" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/services/mim-schedule-gateway" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-identity-sync" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-lifecycle" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-usage-ingest"'
readonly DEFAULT_MIM_FIXED_MAINTENANCE_JOBS_CONDITION='resource.name == "projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-identity-sync" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-lifecycle" || resource.name == "projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-usage-ingest"'

default_project_iam_policy() {
  CONTROL_PLANE_EMAIL="$CONTROL_PLANE_EMAIL" \
  APP_GATEWAY_EMAIL="$APP_GATEWAY_EMAIL" \
  DEPLOY_WORKER_EMAIL="$DEPLOY_WORKER_EMAIL" \
  SCHEDULE_GATEWAY_EMAIL="$SCHEDULE_GATEWAY_EMAIL" \
  MAINTENANCE_EMAIL="$MAINTENANCE_EMAIL" \
  BUILD_EMAIL="$BUILD_EMAIL" \
  ENV_DEFAULT_MIM_RUN_CONDITION="$DEFAULT_MIM_RUN_CONDITION" \
  ENV_DEFAULT_MIM_JOB_CONDITION="$DEFAULT_MIM_JOB_CONDITION" \
  ENV_DEFAULT_MIM_SECRET_CONDITION="$DEFAULT_MIM_SECRET_CONDITION" \
  ENV_DEFAULT_MIM_WORKLOAD_SA_CONDITION="$DEFAULT_MIM_WORKLOAD_SA_CONDITION" \
  RELEASE_EMAIL="$RELEASE_EMAIL" \
  ENV_DEFAULT_MIM_RELEASE_CONDITION="$DEFAULT_MIM_RELEASE_CONDITION" \
  ENV_DEFAULT_MIM_FIXED_MAINTENANCE_JOBS_CONDITION="$DEFAULT_MIM_FIXED_MAINTENANCE_JOBS_CONDITION" \
  python3 - <<'PY'
import json
import os

bindings = [
    {
        "role": "roles/datastore.user",
        "members": [
            f"serviceAccount:{os.environ['CONTROL_PLANE_EMAIL']}",
            f"serviceAccount:{os.environ['DEPLOY_WORKER_EMAIL']}",
            f"serviceAccount:{os.environ['SCHEDULE_GATEWAY_EMAIL']}",
            f"serviceAccount:{os.environ['MAINTENANCE_EMAIL']}",
        ],
    },
    {
        "role": "roles/cloudscheduler.admin",
        "members": [f"serviceAccount:{os.environ['CONTROL_PLANE_EMAIL']}"],
    },
    {
        "role": "roles/cloudbuild.builds.editor",
        "members": [f"serviceAccount:{os.environ['DEPLOY_WORKER_EMAIL']}"],
    },
    {
        "role": "roles/iam.serviceAccountCreator",
        "members": [f"serviceAccount:{os.environ['DEPLOY_WORKER_EMAIL']}"],
    },
    {
        "role": "roles/iam.serviceAccountAdmin",
        "members": [f"serviceAccount:{os.environ['DEPLOY_WORKER_EMAIL']}"],
        "condition": {
            "title": "mim-workload-service-accounts",
            "expression": os.environ["ENV_DEFAULT_MIM_WORKLOAD_SA_CONDITION"],
        },
    },
    {
        "role": "roles/iam.securityReviewer",
        "members": [f"serviceAccount:{os.environ['DEPLOY_WORKER_EMAIL']}"],
        "condition": {
            "title": "mim-workload-service-accounts",
            "expression": os.environ["ENV_DEFAULT_MIM_WORKLOAD_SA_CONDITION"],
        },
    },
    {
        "role": "roles/run.admin",
        "members": [f"serviceAccount:{os.environ['DEPLOY_WORKER_EMAIL']}"],
        "condition": {
            "title": "mim-dynamic-run",
            "expression": os.environ["ENV_DEFAULT_MIM_RUN_CONDITION"],
        },
    },
    {
        "role": "roles/run.viewer",
        "members": [f"serviceAccount:{os.environ['SCHEDULE_GATEWAY_EMAIL']}"],
        "condition": {
            "title": "mim-dynamic-jobs",
            "expression": os.environ["ENV_DEFAULT_MIM_JOB_CONDITION"],
        },
    },
    {
        "role": "roles/run.jobsExecutorWithOverrides",
        "members": [f"serviceAccount:{os.environ['SCHEDULE_GATEWAY_EMAIL']}"],
        "condition": {
            "title": "mim-dynamic-jobs",
            "expression": os.environ["ENV_DEFAULT_MIM_JOB_CONDITION"],
        },
    },
    {
        "role": "roles/secretmanager.admin",
        "members": [f"serviceAccount:{os.environ['CONTROL_PLANE_EMAIL']}"],
        "condition": {
            "title": "mim-managed-secrets",
            "expression": os.environ["ENV_DEFAULT_MIM_SECRET_CONDITION"],
        },
    },
    {
        "role": "roles/bigquery.jobUser",
        "members": [f"serviceAccount:{os.environ['MAINTENANCE_EMAIL']}"],
    },
    {
        "role": "roles/run.jobsExecutor",
        "members": [f"serviceAccount:{os.environ['MAINTENANCE_EMAIL']}"],
        "condition": {
            "title": "mim-fixed-maintenance-jobs",
            "expression": os.environ["ENV_DEFAULT_MIM_FIXED_MAINTENANCE_JOBS_CONDITION"],
        },
    },
    {
        "role": "roles/run.admin",
        "members": [f"serviceAccount:{os.environ['RELEASE_EMAIL']}"],
        "condition": {
            "title": "mim-release-runtimes",
            "expression": os.environ["ENV_DEFAULT_MIM_RELEASE_CONDITION"],
        },
    },
    {
        "role": "roles/cloudscheduler.admin",
        "members": [f"serviceAccount:{os.environ['RELEASE_EMAIL']}"],
    },
]
print(json.dumps({"bindings": bindings}, separators=(",", ":")))
PY
}

default_build_sa_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/iam.serviceAccountUser","members":["serviceAccount:$DEPLOY_WORKER_EMAIL"]}
]}
EOF
}

default_release_act_as_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/iam.serviceAccountUser","members":["serviceAccount:$RELEASE_EMAIL"]}
]}
EOF
}

default_app_gateway_secret_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:$APP_GATEWAY_EMAIL"]}
]}
EOF
}

default_edge_origin_secret_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:$CONTROL_PLANE_EMAIL"]}
]}
EOF
}

default_desired_state_signing_secret_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:$CONTROL_PLANE_EMAIL","serviceAccount:$DEPLOY_WORKER_EMAIL"]}
]}
EOF
}

default_github_webhook_secret_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:$CONTROL_PLANE_EMAIL"]}
]}
EOF
}

default_github_app_key_secret_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:$CONTROL_PLANE_EMAIL","serviceAccount:$DEPLOY_WORKER_EMAIL"]}
]}
EOF
}

default_app_gateway_previous_secret_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:$APP_GATEWAY_EMAIL"]}
]}
EOF
}

default_schedule_gateway_sa_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/iam.serviceAccountUser","members":["serviceAccount:$CONTROL_PLANE_EMAIL","serviceAccount:$RELEASE_EMAIL"]}
]}
EOF
}

default_identity_sync_sa_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/iam.serviceAccountTokenCreator","members":["serviceAccount:$MAINTENANCE_EMAIL"]}
]}
EOF
}

default_release_sa_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/iam.serviceAccountTokenCreator","members":["user:$OPERATOR_EMAIL"]}
]}
EOF
}

default_artifact_repo_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/artifactregistry.writer","members":["serviceAccount:$DEPLOY_WORKER_EMAIL","serviceAccount:$BUILD_EMAIL"]}
]}
EOF
}

default_bootstrap_secret_policy() {
  cat <<EOF
{"bindings":[
  {"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:$CONTROL_PLANE_EMAIL","serviceAccount:$DEPLOY_WORKER_EMAIL","serviceAccount:$SCHEDULE_GATEWAY_EMAIL","serviceAccount:$MAINTENANCE_EMAIL"]},
  {"role":"roles/secretmanager.secretVersionAdder","members":["serviceAccount:$RELEASE_EMAIL"]}
]}
EOF
}

default_bq_dataset_json() {
  cat <<EOF
{"datasetReference":{"projectId":"$PROJECT_ID","datasetId":"mim_billing_export"},"access":[]}
EOF
}

ready_plugin_root() {
  local root=$1
  mkdir -p \
    "$root/control-plane/src/mim_control_plane/workers" \
    "$root/infra/release" \
    "$root/infra/edge"
  : >"$root/control-plane/src/mim_control_plane/workers/deploy.py"
  : >"$root/control-plane/src/mim_control_plane/workers/identity_sync.py"
  : >"$root/control-plane/src/mim_control_plane/machine_api.py"
  : >"$root/control-plane/src/mim_control_plane/runtime.py"
  : >"$root/control-plane/src/mim_control_plane/workers/lifecycle.py"
  : >"$root/control-plane/src/mim_control_plane/workers/reconcile.py"
  : >"$root/control-plane/src/mim_control_plane/workers/usage_ingest.py"
  : >"$root/infra/release/plan.sh"
  : >"$root/infra/release/apply.sh"
  : >"$root/infra/edge/plan.sh"
  : >"$root/infra/edge/apply.sh"
}

assert_contains() {
  local file=$1
  local expected=$2
  local case_name=$3
  if ! grep -Fq -- "$expected" "$file"; then
    printf 'FAIL %s: missing %s\n' "$case_name" "$expected" >&2
    cat "$file" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
}

assert_not_contains() {
  local file=$1
  local unexpected=$2
  local case_name=$3
  if grep -Fq -- "$unexpected" "$file"; then
    printf 'FAIL %s: unexpected %s\n' "$case_name" "$unexpected" >&2
    cat "$file" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
}

assert_line_order() {
  local file=$1
  local first=$2
  local second=$3
  local case_name=$4
  local first_line
  local second_line

  first_line=$(grep -Fn -- "$first" "$file" | head -n 1 | cut -d: -f1 || true)
  second_line=$(grep -Fn -- "$second" "$file" | head -n 1 | cut -d: -f1 || true)
  if [[ -z "$first_line" || -z "$second_line" || "$first_line" -ge "$second_line" ]]; then
    printf 'FAIL %s: expected %s before %s\n' "$case_name" "$first" "$second" >&2
    cat "$file" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
}

assert_body_not_leaked() {
  local file=$1
  local body=$2
  local case_name=$3
  local line

  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    assert_not_contains "$file" "$line" "$case_name"
  done <<<"$body"
}

run_apply_case() {
  local case_name=$1
  local expected_exit=$2
  local expected_substring=$3
  local plugin_root=$4
  shift 4

  local output_path="$TMP_DIR/$case_name.out"
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  local index_state_path="$TMP_DIR/$case_name.indexes.json"
  local ttl_state_path="$TMP_DIR/$case_name.ttls.json"
  write_valid_config "$config_path"
  write_protected_file "$protected_path"
  printf '%s' "${GCLOUD_INDEX_LIST_JSON:-[]}" >"$index_state_path"
  printf '%s' "${GCLOUD_TTL_LIST_JSON:-[]}" >"$ttl_state_path"
  : >"$GCLOUD_LOG"

  set +e
  PATH="$STUB_BIN:$PATH" \
    GCLOUD_LOG="$GCLOUD_LOG" \
    GCLOUD_ACTIVE_ACCOUNT="${GCLOUD_ACTIVE_ACCOUNT:-$OPERATOR_EMAIL}" \
    GCLOUD_PROJECT_ID="${GCLOUD_PROJECT_ID:-$PROJECT_ID}" \
    GCLOUD_PROJECT_PARENT_TYPE="${GCLOUD_PROJECT_PARENT_TYPE:-organization}" \
    GCLOUD_PROJECT_PARENT_ID="${GCLOUD_PROJECT_PARENT_ID:-$ORGANIZATION_ID}" \
    GCLOUD_PROJECT_NUMBER="${GCLOUD_PROJECT_NUMBER:-$PROJECT_NUMBER}" \
    GCLOUD_BILLING_ENABLED="${GCLOUD_BILLING_ENABLED:-True}" \
    GCLOUD_BILLING_ACCOUNT_NAME="${GCLOUD_BILLING_ACCOUNT_NAME:-billingAccounts/$BILLING_ACCOUNT_ID}" \
    GCLOUD_ENABLED_APIS="${GCLOUD_ENABLED_APIS:-}" \
    GCLOUD_PROJECT_RUN_INVOKERS="${GCLOUD_PROJECT_RUN_INVOKERS:-}" \
    GCLOUD_CONTROL_PLANE_ROLES="${GCLOUD_CONTROL_PLANE_ROLES:-}" \
    GCLOUD_APP_GATEWAY_ROLES="${GCLOUD_APP_GATEWAY_ROLES:-}" \
    GCLOUD_DEPLOY_WORKER_ROLES="${GCLOUD_DEPLOY_WORKER_ROLES:-}" \
    GCLOUD_BUILD_ROLES="${GCLOUD_BUILD_ROLES:-}" \
    GCLOUD_SCHEDULE_GATEWAY_ROLES="${GCLOUD_SCHEDULE_GATEWAY_ROLES:-}" \
    GCLOUD_MAINTENANCE_ROLES="${GCLOUD_MAINTENANCE_ROLES:-}" \
    GCLOUD_IDENTITY_SYNC_ROLES="${GCLOUD_IDENTITY_SYNC_ROLES:-}" \
    GCLOUD_RELEASE_ROLES="${GCLOUD_RELEASE_ROLES:-}" \
    GCLOUD_PROJECT_IAM_POLICY_JSON="${GCLOUD_PROJECT_IAM_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON="${GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON:-$(default_artifact_repo_policy)}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS:-exists}" \
    GCLOUD_BUILD_SA_POLICY_JSON="${GCLOUD_BUILD_SA_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_BUILD_SA_POLICY_STATUS="${GCLOUD_BUILD_SA_POLICY_STATUS:-exists}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_JSON="${GCLOUD_CONTROL_PLANE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS="${GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_JSON="${GCLOUD_APP_GATEWAY_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_STATUS="${GCLOUD_APP_GATEWAY_SA_POLICY_STATUS:-exists}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON="${GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS="${GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS:-exists}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON="${GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS="${GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS:-exists}" \
    GCLOUD_MAINTENANCE_SA_POLICY_JSON="${GCLOUD_MAINTENANCE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_MAINTENANCE_SA_POLICY_STATUS="${GCLOUD_MAINTENANCE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_RELEASE_SA_POLICY_JSON="${GCLOUD_RELEASE_SA_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_RELEASE_SA_POLICY_STATUS="${GCLOUD_RELEASE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON="${GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON:-$(default_schedule_gateway_sa_policy)}" \
    GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_STATUS="${GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_STATUS:-exists}" \
    GCLOUD_BOOTSTRAP_SECRET_IAM_JSON="${GCLOUD_BOOTSTRAP_SECRET_IAM_JSON:-$(default_bootstrap_secret_policy)}" \
    GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS="${GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_SECRET_IAM_JSON="${GCLOUD_APP_GATEWAY_SECRET_IAM_JSON:-$(default_app_gateway_secret_policy)}" \
    GCLOUD_APP_GATEWAY_SECRET_IAM_STATUS="${GCLOUD_APP_GATEWAY_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_JSON="${GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_JSON:-$(default_app_gateway_previous_secret_policy)}" \
    GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_STATUS="${GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_STATUS:-missing}" \
    GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON="${GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON:-$(default_edge_origin_secret_policy)}" \
    GCLOUD_EDGE_ORIGIN_SECRET_IAM_STATUS="${GCLOUD_EDGE_ORIGIN_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON="${GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON:-$(default_desired_state_signing_secret_policy)}" \
    GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_STATUS="${GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON="${GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON:-$(default_github_webhook_secret_policy)}" \
    GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_STATUS="${GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON="${GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON:-$(default_github_app_key_secret_policy)}" \
    GCLOUD_GITHUB_APP_KEY_SECRET_IAM_STATUS="${GCLOUD_GITHUB_APP_KEY_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_RUN_SERVICE_STATUS="${GCLOUD_RUN_SERVICE_STATUS:-missing}" \
    GCLOUD_RUN_SERVICE_ACCOUNT="${GCLOUD_RUN_SERVICE_ACCOUNT:-$CONTROL_PLANE_EMAIL}" \
    GCLOUD_MIN_SCALE="${GCLOUD_MIN_SCALE:-0}" \
    GCLOUD_MAX_SCALE="${GCLOUD_MAX_SCALE:-1}" \
    GCLOUD_SERVICE_ACCOUNTS_EXIST="${GCLOUD_SERVICE_ACCOUNTS_EXIST:-missing}" \
    GCLOUD_ARTIFACT_REPOSITORY_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_STATUS:-missing}" \
    GCLOUD_ARTIFACT_REPOSITORY_FORMAT="${GCLOUD_ARTIFACT_REPOSITORY_FORMAT:-DOCKER}" \
    GCLOUD_FIRESTORE_STATUS="${GCLOUD_FIRESTORE_STATUS:-missing}" \
    GCLOUD_FIRESTORE_LOCATION="${GCLOUD_FIRESTORE_LOCATION:-asia-northeast3}" \
    GCLOUD_FIRESTORE_TYPE="${GCLOUD_FIRESTORE_TYPE:-FIRESTORE_NATIVE}" \
    GCLOUD_INDEX_STATE_FILE="$index_state_path" \
    GCLOUD_INDEX_LIST_JSON="${GCLOUD_INDEX_LIST_JSON:-[]}" \
    GCLOUD_INDEX_LIST_STATUS="${GCLOUD_INDEX_LIST_STATUS:-ok}" \
    GCLOUD_INDEX_POST_CREATE_JSON="${GCLOUD_INDEX_POST_CREATE_JSON:-}" \
    GCLOUD_INDEX_CREATE_STATUS="${GCLOUD_INDEX_CREATE_STATUS:-ok}" \
    GCLOUD_TTL_STATE_FILE="$ttl_state_path" \
    GCLOUD_TTL_LIST_JSON="${GCLOUD_TTL_LIST_JSON:-[]}" \
    GCLOUD_TTL_LIST_STATUS="${GCLOUD_TTL_LIST_STATUS:-ok}" \
    GCLOUD_TTL_POST_UPDATE_JSON="${GCLOUD_TTL_POST_UPDATE_JSON:-}" \
    GCLOUD_TTL_UPDATE_STATUS="${GCLOUD_TTL_UPDATE_STATUS:-ok}" \
    GCLOUD_QUEUE_STATUS="${GCLOUD_QUEUE_STATUS:-missing}" \
    GCLOUD_QUEUE_STATE="${GCLOUD_QUEUE_STATE:-RUNNING}" \
    GCLOUD_QUEUE_MAX_ATTEMPTS="${GCLOUD_QUEUE_MAX_ATTEMPTS:-4}" \
    GCLOUD_QUEUE_MAX_RETRY_DURATION="${GCLOUD_QUEUE_MAX_RETRY_DURATION:-300s}" \
    GCLOUD_QUEUE_MIN_BACKOFF="${GCLOUD_QUEUE_MIN_BACKOFF:-5s}" \
    GCLOUD_QUEUE_MAX_BACKOFF="${GCLOUD_QUEUE_MAX_BACKOFF:-60s}" \
    GCLOUD_QUEUE_MAX_DOUBLINGS="${GCLOUD_QUEUE_MAX_DOUBLINGS:-3}" \
    GCLOUD_SECRET_STATUS="${GCLOUD_SECRET_STATUS:-missing}" \
    BQ_DATASET_JSON="${BQ_DATASET_JSON:-$(default_bq_dataset_json)}" \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    MIM_PLUGIN_ROOT="$plugin_root" \
    bash "$APPLY_SCRIPT" "$@" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return 1
  fi

  assert_contains "$output_path" "$expected_substring" "$case_name"
  assert_not_contains "$output_path" "$CLOUDFLARE_ACCOUNT_ID" "$case_name"
  assert_not_contains "$output_path" "$SLACK_APPROVED_WORKSPACE_IDS" "$case_name"
  printf 'PASS %s\n' "$case_name"
  return 0
}

run_apply_case_with_protected_body() {
  local case_name=$1
  local expected_exit=$2
  local expected_substring=$3
  local plugin_root=$4
  local protected_body=$5
  shift 5

  local output_path="$TMP_DIR/$case_name.out"
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  local index_state_path="$TMP_DIR/$case_name.indexes.json"
  local ttl_state_path="$TMP_DIR/$case_name.ttls.json"
  write_valid_config "$config_path"
  write_protected_file "$protected_path" "$protected_body"
  printf '%s' "${GCLOUD_INDEX_LIST_JSON:-[]}" >"$index_state_path"
  printf '%s' "${GCLOUD_TTL_LIST_JSON:-[]}" >"$ttl_state_path"
  : >"$GCLOUD_LOG"

  set +e
  PATH="$STUB_BIN:$PATH" \
    GCLOUD_LOG="$GCLOUD_LOG" \
    GCLOUD_ACTIVE_ACCOUNT="${GCLOUD_ACTIVE_ACCOUNT:-$OPERATOR_EMAIL}" \
    GCLOUD_PROJECT_ID="${GCLOUD_PROJECT_ID:-$PROJECT_ID}" \
    GCLOUD_PROJECT_PARENT_TYPE="${GCLOUD_PROJECT_PARENT_TYPE:-organization}" \
    GCLOUD_PROJECT_PARENT_ID="${GCLOUD_PROJECT_PARENT_ID:-$ORGANIZATION_ID}" \
    GCLOUD_PROJECT_NUMBER="${GCLOUD_PROJECT_NUMBER:-$PROJECT_NUMBER}" \
    GCLOUD_BILLING_ENABLED="${GCLOUD_BILLING_ENABLED:-True}" \
    GCLOUD_BILLING_ACCOUNT_NAME="${GCLOUD_BILLING_ACCOUNT_NAME:-billingAccounts/$BILLING_ACCOUNT_ID}" \
    GCLOUD_ENABLED_APIS="${GCLOUD_ENABLED_APIS:-}" \
    GCLOUD_PROJECT_RUN_INVOKERS="${GCLOUD_PROJECT_RUN_INVOKERS:-}" \
    GCLOUD_CONTROL_PLANE_ROLES="${GCLOUD_CONTROL_PLANE_ROLES:-}" \
    GCLOUD_APP_GATEWAY_ROLES="${GCLOUD_APP_GATEWAY_ROLES:-}" \
    GCLOUD_DEPLOY_WORKER_ROLES="${GCLOUD_DEPLOY_WORKER_ROLES:-}" \
    GCLOUD_BUILD_ROLES="${GCLOUD_BUILD_ROLES:-}" \
    GCLOUD_SCHEDULE_GATEWAY_ROLES="${GCLOUD_SCHEDULE_GATEWAY_ROLES:-}" \
    GCLOUD_MAINTENANCE_ROLES="${GCLOUD_MAINTENANCE_ROLES:-}" \
    GCLOUD_IDENTITY_SYNC_ROLES="${GCLOUD_IDENTITY_SYNC_ROLES:-}" \
    GCLOUD_RELEASE_ROLES="${GCLOUD_RELEASE_ROLES:-}" \
    GCLOUD_PROJECT_IAM_POLICY_JSON="${GCLOUD_PROJECT_IAM_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON="${GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON:-$(default_artifact_repo_policy)}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS:-exists}" \
    GCLOUD_BUILD_SA_POLICY_JSON="${GCLOUD_BUILD_SA_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_BUILD_SA_POLICY_STATUS="${GCLOUD_BUILD_SA_POLICY_STATUS:-exists}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_JSON="${GCLOUD_CONTROL_PLANE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS="${GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_JSON="${GCLOUD_APP_GATEWAY_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_STATUS="${GCLOUD_APP_GATEWAY_SA_POLICY_STATUS:-exists}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON="${GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS="${GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS:-exists}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON="${GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS="${GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS:-exists}" \
    GCLOUD_MAINTENANCE_SA_POLICY_JSON="${GCLOUD_MAINTENANCE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_MAINTENANCE_SA_POLICY_STATUS="${GCLOUD_MAINTENANCE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_RELEASE_SA_POLICY_JSON="${GCLOUD_RELEASE_SA_POLICY_JSON:-{\"bindings\":[]}}" \
    GCLOUD_RELEASE_SA_POLICY_STATUS="${GCLOUD_RELEASE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON="${GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON:-$(default_schedule_gateway_sa_policy)}" \
    GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_STATUS="${GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_STATUS:-exists}" \
    GCLOUD_BOOTSTRAP_SECRET_IAM_JSON="${GCLOUD_BOOTSTRAP_SECRET_IAM_JSON:-$(default_bootstrap_secret_policy)}" \
    GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS="${GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_SECRET_IAM_JSON="${GCLOUD_APP_GATEWAY_SECRET_IAM_JSON:-$(default_app_gateway_secret_policy)}" \
    GCLOUD_APP_GATEWAY_SECRET_IAM_STATUS="${GCLOUD_APP_GATEWAY_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_JSON="${GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_JSON:-$(default_app_gateway_previous_secret_policy)}" \
    GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_STATUS="${GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_STATUS:-missing}" \
    GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON="${GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON:-$(default_edge_origin_secret_policy)}" \
    GCLOUD_EDGE_ORIGIN_SECRET_IAM_STATUS="${GCLOUD_EDGE_ORIGIN_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON="${GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON:-$(default_desired_state_signing_secret_policy)}" \
    GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_STATUS="${GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON="${GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON:-$(default_github_webhook_secret_policy)}" \
    GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_STATUS="${GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON="${GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON:-$(default_github_app_key_secret_policy)}" \
    GCLOUD_GITHUB_APP_KEY_SECRET_IAM_STATUS="${GCLOUD_GITHUB_APP_KEY_SECRET_IAM_STATUS:-exists}" \
    GCLOUD_RUN_SERVICE_STATUS="${GCLOUD_RUN_SERVICE_STATUS:-missing}" \
    GCLOUD_RUN_SERVICE_ACCOUNT="${GCLOUD_RUN_SERVICE_ACCOUNT:-$CONTROL_PLANE_EMAIL}" \
    GCLOUD_MIN_SCALE="${GCLOUD_MIN_SCALE:-0}" \
    GCLOUD_MAX_SCALE="${GCLOUD_MAX_SCALE:-1}" \
    GCLOUD_SERVICE_ACCOUNTS_EXIST="${GCLOUD_SERVICE_ACCOUNTS_EXIST:-missing}" \
    GCLOUD_ARTIFACT_REPOSITORY_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_STATUS:-missing}" \
    GCLOUD_ARTIFACT_REPOSITORY_FORMAT="${GCLOUD_ARTIFACT_REPOSITORY_FORMAT:-DOCKER}" \
    GCLOUD_FIRESTORE_STATUS="${GCLOUD_FIRESTORE_STATUS:-missing}" \
    GCLOUD_FIRESTORE_LOCATION="${GCLOUD_FIRESTORE_LOCATION:-asia-northeast3}" \
    GCLOUD_FIRESTORE_TYPE="${GCLOUD_FIRESTORE_TYPE:-FIRESTORE_NATIVE}" \
    GCLOUD_INDEX_STATE_FILE="$index_state_path" \
    GCLOUD_INDEX_LIST_JSON="${GCLOUD_INDEX_LIST_JSON:-[]}" \
    GCLOUD_INDEX_LIST_STATUS="${GCLOUD_INDEX_LIST_STATUS:-ok}" \
    GCLOUD_INDEX_POST_CREATE_JSON="${GCLOUD_INDEX_POST_CREATE_JSON:-}" \
    GCLOUD_INDEX_CREATE_STATUS="${GCLOUD_INDEX_CREATE_STATUS:-ok}" \
    GCLOUD_TTL_STATE_FILE="$ttl_state_path" \
    GCLOUD_TTL_LIST_JSON="${GCLOUD_TTL_LIST_JSON:-[]}" \
    GCLOUD_TTL_LIST_STATUS="${GCLOUD_TTL_LIST_STATUS:-ok}" \
    GCLOUD_TTL_POST_UPDATE_JSON="${GCLOUD_TTL_POST_UPDATE_JSON:-}" \
    GCLOUD_TTL_UPDATE_STATUS="${GCLOUD_TTL_UPDATE_STATUS:-ok}" \
    GCLOUD_QUEUE_STATUS="${GCLOUD_QUEUE_STATUS:-missing}" \
    GCLOUD_QUEUE_STATE="${GCLOUD_QUEUE_STATE:-RUNNING}" \
    GCLOUD_QUEUE_MAX_ATTEMPTS="${GCLOUD_QUEUE_MAX_ATTEMPTS:-4}" \
    GCLOUD_QUEUE_MAX_RETRY_DURATION="${GCLOUD_QUEUE_MAX_RETRY_DURATION:-300s}" \
    GCLOUD_QUEUE_MIN_BACKOFF="${GCLOUD_QUEUE_MIN_BACKOFF:-5s}" \
    GCLOUD_QUEUE_MAX_BACKOFF="${GCLOUD_QUEUE_MAX_BACKOFF:-60s}" \
    GCLOUD_QUEUE_MAX_DOUBLINGS="${GCLOUD_QUEUE_MAX_DOUBLINGS:-3}" \
    GCLOUD_SECRET_STATUS="${GCLOUD_SECRET_STATUS:-missing}" \
    BQ_DATASET_JSON="${BQ_DATASET_JSON:-$(default_bq_dataset_json)}" \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    MIM_PLUGIN_ROOT="$plugin_root" \
    bash "$APPLY_SCRIPT" "$@" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return 1
  fi

  assert_contains "$output_path" "$expected_substring" "$case_name"
  assert_not_contains "$output_path" "$CLOUDFLARE_ACCOUNT_ID" "$case_name"
  assert_not_contains "$output_path" "$SLACK_APPROVED_WORKSPACE_IDS" "$case_name"
  assert_body_not_leaked "$output_path" "$protected_body" "$case_name"
  printf 'PASS %s\n' "$case_name"
  return 0
}

REAL_PLUGIN_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
READY_PLUGIN_ROOT="$TMP_DIR/plugin-root"
MISSING_WORKER_PLUGIN_ROOT="$TMP_DIR/plugin-root-missing-worker"
ready_plugin_root "$READY_PLUGIN_ROOT"
cp -R "$READY_PLUGIN_ROOT" "$MISSING_WORKER_PLUGIN_ROOT"
rm -f "$MISSING_WORKER_PLUGIN_ROOT/control-plane/src/mim_control_plane/workers/usage_ingest.py"

PLAN_PATH_READY="$STATE_DIR/task17-ready-plan.json"
PLAN_PATH_BLOCKED="$STATE_DIR/task17-blocked-plan.json"

run_apply_case rejects_path_outside_literal_state 1 "Plan output must stay inside the literal .state directory" "$READY_PLUGIN_ROOT" --plan --out "$TMP_DIR/outside.json"

ln -sf "$TMP_DIR/symlink-target" "$STATE_DIR/task17-symlink.json"
run_apply_case rejects_symlink_target 1 "Plan output target must not be a symlink" "$READY_PLUGIN_ROOT" --plan --out "$STATE_DIR/task17-symlink.json"

write_private_file "$STATE_DIR/task17-existing.json" "{}"
run_apply_case rejects_existing_target 1 "Refusing to overwrite existing reviewed plan" "$READY_PLUGIN_ROOT" --plan --out "$STATE_DIR/task17-existing.json"

GCLOUD_QUEUE_STATUS=exists \
GCLOUD_QUEUE_MAX_ATTEMPTS=5 \
run_apply_case rejects_private_queue_retry_drift 1 "Private worker queue max attempts must remain 4" "$READY_PLUGIN_ROOT"

run_apply_case writes_blocked_plan_for_missing_worker_artifacts 0 "Wrote reviewed plan" "$MISSING_WORKER_PLUGIN_ROOT" --plan --out "$PLAN_PATH_BLOCKED"
python3 - "$PLAN_PATH_BLOCKED" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
assert data["status"] == "blocked", data
codes = {item["code"] for item in data["blockers"]}
assert "missing-worker-artifact" in codes, codes
PY

BASE_PROTECTED_BODY="$PRIVATE_PROTECTED_A"$'\n'"$PRIVATE_PROTECTED_B"$'\n'
GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[]}' \
GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON='{"bindings":[]}' \
GCLOUD_BOOTSTRAP_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_APP_GATEWAY_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_BUILD_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_CONTROL_PLANE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_MAINTENANCE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_RELEASE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON='{"bindings":[]}' \
run_apply_case_with_protected_body writes_exact_ready_plan 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$PLAN_PATH_READY"
python3 - "$PLAN_PATH_READY" "$TMP_DIR/writes_exact_ready_plan.env" "$TMP_DIR/writes_exact_ready_plan.protected" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
config_text = Path(sys.argv[2]).read_text()
protected_text = Path(sys.argv[3]).read_text()
expected_keys = {
    "actions",
    "blockers",
    "config",
    "constraints",
    "discovery_hash",
    "expires_at_epoch",
    "generated_at_epoch",
    "iam_contract",
    "initial_state",
    "managed_service_accounts",
    "private_worker_expectations",
    "required_apis",
    "required_secrets",
    "status",
    "targets",
    "version",
}
assert set(plan.keys()) == expected_keys, set(plan.keys())
assert plan["version"] == "mim-control-plane-plan-v2"
assert plan["status"] == "ready"
assert plan["config"]["operator_email"] == "operator.test@madup.com"
assert plan["config"]["project_id"] == "mim-prod-123456"
assert plan["config"]["organization_id"] == "123456789012"
assert plan["config"]["billing_account_id"] == "ABCDEF-123456-7890AB"
assert plan["config"]["config_fingerprint"] == hashlib.sha256(config_text.encode()).hexdigest()
assert plan["config"]["protected_projects_fingerprint"] == hashlib.sha256(protected_text.encode()).hexdigest()
assert "iap.googleapis.com" in plan["required_apis"]
assert "iap.googleapis.com" in plan["initial_state"]["enabled_apis"]["missing"]
assert plan["targets"]["region"] == "asia-northeast3"
assert plan["targets"]["service_name"] == "mim-control-plane"
assert plan["targets"]["artifact_repository"] == "mim-control-plane"
assert plan["targets"]["tasks_queue"] == "mim-private-workers"
assert plan["targets"]["firestore_database"] == "(default)"
assert plan["targets"]["firestore_operations_dashboard_index"] == {
    "collection_group": "operations",
    "database": "(default)",
    "fields": [
        {"field_path": "workload_owner_id", "order": "ASCENDING"},
        {"field_path": "workload_id", "order": "ASCENDING"},
        {"field_path": "updated_at", "order": "DESCENDING"},
        {"field_path": "created_at", "order": "DESCENDING"},
        {"field_path": "id", "order": "DESCENDING"},
    ],
    "query_scope": "COLLECTION",
}
assert plan["targets"]["firestore_replay_claim_ttl"] == {
    "collection_group": "origin_request_claims",
    "database": "(default)",
    "field_path": "expires_at",
}
assert plan["managed_service_accounts"]["control_plane"] == "mim-control-plane@mim-prod-123456.iam.gserviceaccount.com"
assert plan["managed_service_accounts"]["maintenance"] == "mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"
assert "mim-runtime@" not in json.dumps(plan)
assert plan["required_secrets"] == [
    "mim-app-gateway-origin-v1",
    "mim-desired-state-signing",
    "mim-edge-origin-v1",
    "mim-github-app-key",
    "mim-github-webhook",
    "mim-runtime-bootstrap",
]
assert plan["constraints"]["service_mutations_disabled"] is True
assert plan["constraints"]["transport_mutations_disabled"] is True
assert plan["private_worker_expectations"]["oidc_service_account"] == "mim-deploy-worker@mim-prod-123456.iam.gserviceaccount.com"
assert "project" in plan["initial_state"]["iam"]
assert "artifact_repository" in plan["initial_state"]["iam"]
assert "service_accounts" in plan["initial_state"]["iam"]
assert plan["private_worker_expectations"]["queue_retry"] == {
    "max_attempts": 4,
    "max_retry_duration": "300s",
    "min_backoff": "5s",
    "max_backoff": "60s",
    "max_doublings": 3,
}
assert plan["initial_state"]["artifact_repository"]["status"] == "missing"
assert plan["initial_state"]["firestore_database"]["status"] == "missing"
assert plan["initial_state"]["firestore_operations_dashboard_index"] == {
    "collection_group": "operations",
    "database": "(default)",
    "fields": [
        {"field_path": "workload_owner_id", "order": "ASCENDING"},
        {"field_path": "workload_id", "order": "ASCENDING"},
        {"field_path": "updated_at", "order": "DESCENDING"},
        {"field_path": "created_at", "order": "DESCENDING"},
        {"field_path": "id", "order": "DESCENDING"},
    ],
    "query_scope": "COLLECTION",
    "status": "missing",
}
assert plan["initial_state"]["firestore_replay_claim_ttl"] == {
    "collection_group": "origin_request_claims",
    "database": "(default)",
    "field_path": "expires_at",
    "status": "missing",
}
assert plan["initial_state"]["tasks_queue"]["status"] == "missing"
assert plan["initial_state"]["control_plane_service"]["status"] == "missing"
actions = [item["kind"] for item in plan["actions"]]
assert "enable_api" in actions
assert "create_service_account" in actions
assert "bind_project_role" in actions
assert "bind_artifact_repository_role" in actions
assert "bind_secret_resource_role" in actions
assert "bind_service_account_role" in actions
assert "create_artifact_repository" in actions
assert "create_firestore_database" in actions
assert "create_firestore_operations_dashboard_index" in actions
assert "enable_firestore_replay_claim_ttl" in actions
assert "create_tasks_queue" in actions
assert "create_secret" in actions
created_secrets = {
    item["name"] for item in plan["actions"] if item["kind"] == "create_secret"
}
assert created_secrets == {
    "mim-runtime-bootstrap",
    "mim-edge-origin-v1",
    "mim-app-gateway-origin-v1",
    "mim-desired-state-signing",
    "mim-github-webhook",
    "mim-github-app-key",
}
assert "mim-app-gateway-origin-v0" not in created_secrets
assert "mim-origin-current" not in created_secrets
assert "mim-origin-next" not in created_secrets
assert "mim-slack-client-secret" not in created_secrets
index_action = next(item for item in plan["actions"] if item["kind"] == "create_firestore_operations_dashboard_index")
assert index_action["database"] == "(default)"
assert index_action["collection_group"] == "operations"
assert index_action["query_scope"] == "COLLECTION"
assert index_action["fields"] == [
    {"field_path": "workload_owner_id", "order": "ASCENDING"},
    {"field_path": "workload_id", "order": "ASCENDING"},
    {"field_path": "updated_at", "order": "DESCENDING"},
    {"field_path": "created_at", "order": "DESCENDING"},
    {"field_path": "id", "order": "DESCENDING"},
]
assert index_action["before_state"] == {
    "collection_group": "operations",
    "database": "(default)",
    "fields": [
        {"field_path": "workload_owner_id", "order": "ASCENDING"},
        {"field_path": "workload_id", "order": "ASCENDING"},
        {"field_path": "updated_at", "order": "DESCENDING"},
        {"field_path": "created_at", "order": "DESCENDING"},
        {"field_path": "id", "order": "DESCENDING"},
    ],
    "query_scope": "COLLECTION",
    "status": "missing",
}
assert index_action["before_state_hash"] == hashlib.sha256(
    json.dumps(index_action["before_state"], sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
ttl_action = next(item for item in plan["actions"] if item["kind"] == "enable_firestore_replay_claim_ttl")
assert ttl_action["database"] == "(default)"
assert ttl_action["collection_group"] == "origin_request_claims"
assert ttl_action["field_path"] == "expires_at"
assert ttl_action["before_state"] == {
    "collection_group": "origin_request_claims",
    "database": "(default)",
    "field_path": "expires_at",
    "status": "missing",
}
assert ttl_action["before_state_hash"] == hashlib.sha256(
    json.dumps(ttl_action["before_state"], sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
create_db_index = actions.index("create_firestore_database")
create_dashboard_index = actions.index("create_firestore_operations_dashboard_index")
ttl_index = actions.index("enable_firestore_replay_claim_ttl")
assert create_db_index < create_dashboard_index < ttl_index, actions
PY

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_INDEX_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_READY","queryScope":"COLLECTION","state":"READY","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"},{"fieldPath":"__name__","order":"DESCENDING"}]}]' \
GCLOUD_TTL_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/origin_request_claims/fields/expires_at","state":"CREATING","ttlConfig":{"expirationOffset":"0s"}}]' \
run_apply_case_with_protected_body writes_ready_plan_without_ttl_action_when_creating 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-ttl-creating-plan.json"
assert_contains "$GCLOUD_LOG" "firestore indexes composite list --format=json --account=$OPERATOR_EMAIL --project=$PROJECT_ID --database=(default)" writes_ready_plan_without_ttl_action_when_creating
assert_contains "$GCLOUD_LOG" "firestore fields ttls list --collection-group=origin_request_claims" writes_ready_plan_without_ttl_action_when_creating
python3 - "$TMP_DIR/writes_ready_plan_without_ttl_action_when_creating.indexes.json" "$TMP_DIR/writes_ready_plan_without_ttl_action_when_creating.ttls.json" <<'PY'
import json
import sys
from pathlib import Path

index_payload = json.loads(Path(sys.argv[1]).read_text())
ttl_payload = json.loads(Path(sys.argv[2]).read_text())
assert index_payload[0]["state"] == "READY", index_payload
assert ttl_payload[0]["state"] == "CREATING", ttl_payload
PY
python3 - "$STATE_DIR/task17-ttl-creating-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready", plan
index_state = plan["initial_state"]["firestore_operations_dashboard_index"]
if index_state.get("status") != "configured":
    raise RuntimeError(index_state)
if index_state.get("index_state") != "READY":
    raise RuntimeError(index_state)
ttl_state = plan["initial_state"]["firestore_replay_claim_ttl"]
if ttl_state.get("status") != "configured":
    raise RuntimeError(ttl_state)
if ttl_state.get("ttl_state") != "CREATING":
    raise RuntimeError(ttl_state)
assert not any(item["kind"] == "create_firestore_operations_dashboard_index" for item in plan["actions"]), plan["actions"]
assert not any(item["kind"] == "enable_firestore_replay_claim_ttl" for item in plan["actions"]), plan["actions"]
PY
rm -f "$STATE_DIR/task17-ttl-creating-plan.json" "$STATE_DIR/task17-ttl-creating-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_INDEX_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_CREATING","queryScope":"COLLECTION","state":"CREATING","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"},{"fieldPath":"__name__","order":"DESCENDING"}]}]' \
run_apply_case_with_protected_body writes_blocked_plan_while_operations_index_creating 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-index-creating-plan.json"
python3 - "$STATE_DIR/task17-index-creating-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked", plan
codes = {item["code"] for item in plan["blockers"]}
assert "firestore-operations-dashboard-index-invalid" in codes, codes
assert plan["initial_state"]["firestore_operations_dashboard_index"]["index_state"] == "CREATING"
PY
rm -f "$STATE_DIR/task17-index-creating-plan.json" "$STATE_DIR/task17-index-creating-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_INDEX_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_REPAIR","queryScope":"COLLECTION","state":"NEEDS_REPAIR","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"}]}]' \
run_apply_case_with_protected_body writes_blocked_plan_for_repairing_operations_index 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-index-repair-plan.json"
python3 - "$STATE_DIR/task17-index-repair-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked", plan
codes = {item["code"] for item in plan["blockers"]}
assert "firestore-operations-dashboard-index-invalid" in codes, codes
PY
rm -f "$STATE_DIR/task17-index-repair-plan.json" "$STATE_DIR/task17-index-repair-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_INDEX_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_DUP_A","queryScope":"COLLECTION","state":"READY","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"}]},{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_DUP_B","queryScope":"COLLECTION","state":"READY","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"},{"fieldPath":"__name__","order":"DESCENDING"}]}]' \
run_apply_case_with_protected_body writes_blocked_plan_for_duplicate_operations_indexes 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-index-duplicate-plan.json"
python3 - "$STATE_DIR/task17-index-duplicate-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked", plan
codes = {item["code"] for item in plan["blockers"]}
assert "firestore-operations-dashboard-index-invalid" in codes, codes
PY
rm -f "$STATE_DIR/task17-index-duplicate-plan.json" "$STATE_DIR/task17-index-duplicate-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_INDEX_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_READY","queryScope":"COLLECTION","state":"READY","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"}]},{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_AMBIG","queryScope":"COLLECTION","state":"READY","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"},{"fieldPath":"priority","order":"DESCENDING"}]}]' \
run_apply_case_with_protected_body writes_blocked_plan_for_ambiguous_operations_indexes 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-index-ambiguous-plan.json"
python3 - "$STATE_DIR/task17-index-ambiguous-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked", plan
codes = {item["code"] for item in plan["blockers"]}
assert "firestore-operations-dashboard-index-invalid" in codes, codes
PY
rm -f "$STATE_DIR/task17-index-ambiguous-plan.json" "$STATE_DIR/task17-index-ambiguous-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_INDEX_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_WRONG_FIELDS","queryScope":"COLLECTION","state":"READY","fields":[{"fieldPath":"created_at","order":"ASCENDING"}]}]' \
run_apply_case_with_protected_body writes_blocked_plan_for_wrong_operations_index_fields 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-index-wrong-fields-plan.json"
python3 - "$STATE_DIR/task17-index-wrong-fields-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked", plan
codes = {item["code"] for item in plan["blockers"]}
assert "firestore-operations-dashboard-index-invalid" in codes, codes
PY
rm -f "$STATE_DIR/task17-index-wrong-fields-plan.json" "$STATE_DIR/task17-index-wrong-fields-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_TTL_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/origin_request_claims/fields/other_field","state":"ACTIVE"}]' \
run_apply_case_with_protected_body writes_blocked_plan_for_wrong_ttl_field 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-ttl-wrong-field-plan.json"
python3 - "$STATE_DIR/task17-ttl-wrong-field-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
codes = {item["code"] for item in plan["blockers"]}
summary = {"status": plan["status"], "blocker_codes": sorted(codes)}
if plan["status"] != "blocked":
    raise RuntimeError(summary)
if "firestore-replay-claim-ttl-invalid" not in codes:
    raise RuntimeError(summary)
PY
rm -f "$STATE_DIR/task17-ttl-wrong-field-plan.json" "$STATE_DIR/task17-ttl-wrong-field-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_TTL_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/origin_request_claims/fields/expires_at","state":"ACTIVE","ttlConfig":{"expirationOffset":"60s"}}]' \
run_apply_case_with_protected_body writes_blocked_plan_for_nonzero_ttl_offset 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-ttl-offset-plan.json"
python3 - "$STATE_DIR/task17-ttl-offset-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked", plan
codes = {item["code"] for item in plan["blockers"]}
assert "firestore-replay-claim-ttl-invalid" in codes, codes
PY
rm -f "$STATE_DIR/task17-ttl-offset-plan.json" "$STATE_DIR/task17-ttl-offset-plan.json.sha256"

GCLOUD_ENABLED_APIS=firestore.googleapis.com \
GCLOUD_FIRESTORE_STATUS=exists \
GCLOUD_TTL_LIST_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/origin_request_claims/fields/expires_at","state":"ACTIVE"},{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/origin_request_claims/fields/other_field","state":"ACTIVE"}]' \
run_apply_case_with_protected_body writes_blocked_plan_for_duplicate_ttl_fields 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-ttl-duplicate-plan.json"
python3 - "$STATE_DIR/task17-ttl-duplicate-plan.json" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked", plan
codes = {item["code"] for item in plan["blockers"]}
assert "firestore-replay-claim-ttl-invalid" in codes, codes
PY
rm -f "$STATE_DIR/task17-ttl-duplicate-plan.json" "$STATE_DIR/task17-ttl-duplicate-plan.json.sha256"

run_apply_case rejects_blocked_plan_on_apply 1 "Reviewed plan contains blockers" "$MISSING_WORKER_PLUGIN_ROOT" --apply --plan-file "$PLAN_PATH_BLOCKED"

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[]}' \
GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON='{"bindings":[]}' \
GCLOUD_BOOTSTRAP_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_APP_GATEWAY_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_BUILD_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_CONTROL_PLANE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_MAINTENANCE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_RELEASE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_INDEX_POST_CREATE_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/operations/indexes/IDX_READY","queryScope":"COLLECTION","state":"READY","fields":[{"fieldPath":"workload_owner_id","order":"ASCENDING"},{"fieldPath":"workload_id","order":"ASCENDING"},{"fieldPath":"updated_at","order":"DESCENDING"},{"fieldPath":"created_at","order":"DESCENDING"},{"fieldPath":"id","order":"DESCENDING"},{"fieldPath":"__name__","order":"DESCENDING"}]}]' \
GCLOUD_TTL_POST_UPDATE_JSON='[{"name":"projects/mim-prod-123456/databases/(default)/collectionGroups/origin_request_claims/fields/expires_at","state":"CREATING","ttlConfig":{"expirationOffset":"0s"}}]' \
run_apply_case_with_protected_body applies_ready_plan 0 "Applied reviewed plan." "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --apply --plan-file "$PLAN_PATH_READY"
assert_contains "$GCLOUD_LOG" "services enable run.googleapis.com" applies_ready_plan
assert_contains "$GCLOUD_LOG" "iap.googleapis.com" applies_ready_plan
assert_contains "$GCLOUD_LOG" "iam service-accounts create mim-control-plane" applies_ready_plan
assert_contains "$GCLOUD_LOG" "iam service-accounts create mim-maintenance" applies_ready_plan
assert_contains "$GCLOUD_LOG" "projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$CONTROL_PLANE_EMAIL --role=roles/datastore.user" applies_ready_plan
assert_contains "$GCLOUD_LOG" "--role=roles/secretmanager.admin --condition=expression=resource.name.startsWith(\"projects/$PROJECT_ID/secrets/mim-sec-\"),title=mim-managed-secrets" applies_ready_plan
assert_contains "$GCLOUD_LOG" "iam service-accounts add-iam-policy-binding $IDENTITY_SYNC_EMAIL --member=serviceAccount:$MAINTENANCE_EMAIL --role=roles/iam.serviceAccountTokenCreator" applies_ready_plan
assert_contains "$GCLOUD_LOG" "iam service-accounts add-iam-policy-binding $CONTROL_PLANE_EMAIL --member=serviceAccount:$RELEASE_EMAIL --role=roles/iam.serviceAccountUser" applies_ready_plan
assert_contains "$GCLOUD_LOG" "artifacts repositories add-iam-policy-binding mim --location=asia-northeast3 --member=serviceAccount:$DEPLOY_WORKER_EMAIL --role=roles/artifactregistry.writer" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-runtime-bootstrap --member=serviceAccount:$CONTROL_PLANE_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-runtime-bootstrap --member=serviceAccount:$DEPLOY_WORKER_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-runtime-bootstrap --member=serviceAccount:$SCHEDULE_GATEWAY_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-runtime-bootstrap --member=serviceAccount:$MAINTENANCE_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-runtime-bootstrap --member=serviceAccount:$RELEASE_EMAIL --role=roles/secretmanager.secretVersionAdder" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-edge-origin-v1 --member=serviceAccount:$CONTROL_PLANE_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-app-gateway-origin-v1 --member=serviceAccount:$APP_GATEWAY_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-desired-state-signing --member=serviceAccount:$CONTROL_PLANE_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-desired-state-signing --member=serviceAccount:$DEPLOY_WORKER_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-github-webhook --member=serviceAccount:$CONTROL_PLANE_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-github-app-key --member=serviceAccount:$CONTROL_PLANE_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
assert_contains "$GCLOUD_LOG" "secrets add-iam-policy-binding mim-github-app-key --member=serviceAccount:$DEPLOY_WORKER_EMAIL --role=roles/secretmanager.secretAccessor" applies_ready_plan
for fixed_secret in mim-runtime-bootstrap mim-edge-origin-v1 mim-app-gateway-origin-v1 mim-desired-state-signing mim-github-webhook mim-github-app-key; do
  assert_contains "$GCLOUD_LOG" "secrets create $fixed_secret" applies_ready_plan
  assert_line_order "$GCLOUD_LOG" "secrets create $fixed_secret" "secrets add-iam-policy-binding $fixed_secret" applies_ready_plan
done
assert_not_contains "$GCLOUD_LOG" "secrets create mim-app-gateway-origin-v0" applies_ready_plan
assert_not_contains "$GCLOUD_LOG" "secrets create mim-origin-current" applies_ready_plan
assert_not_contains "$GCLOUD_LOG" "secrets create mim-origin-next" applies_ready_plan
assert_not_contains "$GCLOUD_LOG" "secrets create mim-slack-client-secret" applies_ready_plan
assert_not_contains "$GCLOUD_LOG" "projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$DEPLOY_WORKER_EMAIL --role=roles/secretmanager.admin" applies_ready_plan
assert_contains "$GCLOUD_LOG" "artifacts repositories create mim-control-plane" applies_ready_plan
assert_contains "$GCLOUD_LOG" "firestore databases create" applies_ready_plan
assert_contains "$GCLOUD_LOG" "firestore indexes composite create --field-config=field-path=workload_owner_id,order=ascending --field-config=field-path=workload_id,order=ascending --field-config=field-path=updated_at,order=descending --field-config=field-path=created_at,order=descending --field-config=field-path=id,order=descending --database=(default) --collection-group=operations --query-scope=collection --account=$OPERATOR_EMAIL --project=$PROJECT_ID" applies_ready_plan
assert_not_contains "$GCLOUD_LOG" "firestore indexes composite create --field-config=field-path=workload_owner_id,order=ascending --field-config=field-path=workload_id,order=ascending --field-config=field-path=updated_at,order=descending --field-config=field-path=created_at,order=descending --field-config=field-path=id,order=descending --database=(default) --collection-group=operations --query-scope=collection --async" applies_ready_plan
assert_contains "$GCLOUD_LOG" "firestore indexes composite list --format=json --account=$OPERATOR_EMAIL --project=$PROJECT_ID --database=(default)" applies_ready_plan
assert_contains "$GCLOUD_LOG" "firestore fields ttls update expires_at --collection-group=origin_request_claims --database=(default) --enable-ttl --async --quiet --account=$OPERATOR_EMAIL --project=$PROJECT_ID" applies_ready_plan
assert_contains "$GCLOUD_LOG" "firestore fields ttls list --collection-group=origin_request_claims --format=json --account=$OPERATOR_EMAIL --project=$PROJECT_ID --database=(default)" applies_ready_plan
assert_line_order "$GCLOUD_LOG" "firestore databases create (default)" "firestore indexes composite create" applies_ready_plan
assert_line_order "$GCLOUD_LOG" "firestore indexes composite create" "firestore fields ttls update expires_at" applies_ready_plan
assert_contains "$GCLOUD_LOG" "tasks queues create mim-private-workers" applies_ready_plan
assert_contains "$GCLOUD_LOG" "--max-attempts=4" applies_ready_plan
assert_contains "$GCLOUD_LOG" "--max-retry-duration=300s" applies_ready_plan
assert_contains "$GCLOUD_LOG" "--min-backoff=5s" applies_ready_plan
assert_contains "$GCLOUD_LOG" "--max-backoff=60s" applies_ready_plan
assert_contains "$GCLOUD_LOG" "--max-doublings=3" applies_ready_plan
assert_not_contains "$GCLOUD_LOG" "run deploy" applies_ready_plan

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[]}' \
GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON='{"bindings":[]}' \
GCLOUD_BOOTSTRAP_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_APP_GATEWAY_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_DESIRED_STATE_SIGNING_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON='{"bindings":[]}' \
GCLOUD_BUILD_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_CONTROL_PLANE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_MAINTENANCE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_RELEASE_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_JSON='{"bindings":[]}' \
GCLOUD_INDEX_LIST_JSON='[]' \
GCLOUD_INDEX_POST_CREATE_JSON='[]' \
GCLOUD_TTL_LIST_JSON='[]' \
GCLOUD_TTL_POST_UPDATE_JSON='[]' \
run_apply_case_with_protected_body rejects_unconverged_index_readback 1 "Readback verification failed" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --apply --plan-file "$PLAN_PATH_READY"

GCLOUD_BUILD_SA_POLICY_STATUS=missing \
GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS=missing \
GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS=missing \
GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS=missing \
GCLOUD_MAINTENANCE_SA_POLICY_STATUS=missing \
GCLOUD_RELEASE_SA_POLICY_STATUS=missing \
GCLOUD_SCHEDULE_GATEWAY_SA_POLICY_STATUS=missing \
GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS=missing \
GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS=missing \
BQ_DATASET_STATUS=missing \
run_apply_case_with_protected_body writes_ready_plan_with_missing_iam_targets 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$STATE_DIR/task17-missing-iam-plan.json"
rm -f "$STATE_DIR/task17-missing-iam-plan.json" "$STATE_DIR/task17-missing-iam-plan.json.sha256"

BQ_DATASET_STATUS=error \
run_apply_case_with_protected_body rejects_raw_billing_export_read_error 1 "Unable to inspect raw billing export dataset" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY"

BQ_DATASET_STATUS=misleading_not_found \
run_apply_case_with_protected_body rejects_misleading_not_found_billing_export_error 1 "Unable to inspect raw billing export dataset" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY"

GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS=error \
run_apply_case_with_protected_body rejects_bootstrap_secret_iam_read_error 1 "Unable to inspect Secret Manager IAM policy for secret mim-runtime-bootstrap" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY"

GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS=misleading_not_found \
run_apply_case_with_protected_body rejects_misleading_secret_not_found 1 "Unable to inspect Secret Manager IAM policy for secret mim-runtime-bootstrap" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY"

python3 - "$PLAN_PATH_READY" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["generated_at_epoch"] = data["generated_at_epoch"] + 4000
data["expires_at_epoch"] = data["generated_at_epoch"] + 1800
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
printf '%s  %s\n' "$(LC_ALL=C LANG=C shasum -a 256 "$PLAN_PATH_READY" | awk '{print $1}')" "$(basename "$PLAN_PATH_READY")" >"$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body rejects_future_generated_at 1 "Plan generated_at cannot be in the future" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --apply --plan-file "$PLAN_PATH_READY"

rm -f "$PLAN_PATH_READY" "$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body writes_exact_ready_plan_again 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$PLAN_PATH_READY"
python3 - "$PLAN_PATH_READY" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["expires_at_epoch"] = data["generated_at_epoch"] + 1900
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
printf '%s  %s\n' "$(LC_ALL=C LANG=C shasum -a 256 "$PLAN_PATH_READY" | awk '{print $1}')" "$(basename "$PLAN_PATH_READY")" >"$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body rejects_bad_expiry_window 1 "Plan expiry must be exactly 1800 seconds after generation" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --apply --plan-file "$PLAN_PATH_READY"

rm -f "$PLAN_PATH_READY" "$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body writes_exact_ready_plan_third 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$PLAN_PATH_READY"
python3 - "$PLAN_PATH_READY" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["managed_service_accounts"]["control_plane"] = data["managed_service_accounts"]["control_plane"].replace("mim-control-plane", "mim-runtime")
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
printf '%s  %s\n' "$(LC_ALL=C LANG=C shasum -a 256 "$PLAN_PATH_READY" | awk '{print $1}')" "$(basename "$PLAN_PATH_READY")" >"$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body rejects_tampered_service_account 1 "Plan file does not match the expected reviewed contract" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --apply --plan-file "$PLAN_PATH_READY"

rm -f "$PLAN_PATH_READY" "$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body writes_exact_ready_plan_fourth 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$PLAN_PATH_READY"
GCLOUD_ENABLED_APIS="run.googleapis.com" \
run_apply_case_with_protected_body rejects_drifted_initial_state 1 "Discovery drift detected" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --apply --plan-file "$PLAN_PATH_READY"

rm -f "$PLAN_PATH_READY" "$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body writes_exact_ready_plan_fifth 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$PLAN_PATH_READY"
run_apply_case_with_protected_body rejects_protected_addition 1 "Protected project fingerprint mismatch" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY$PRIVATE_PROTECTED_C"$'\n' --apply --plan-file "$PLAN_PATH_READY"
run_apply_case_with_protected_body rejects_protected_removal 1 "Protected project fingerprint mismatch" "$READY_PLUGIN_ROOT" "$PRIVATE_PROTECTED_A"$'\n' --apply --plan-file "$PLAN_PATH_READY"
rm -f "$PLAN_PATH_READY" "$PLAN_PATH_READY.sha256"
run_apply_case_with_protected_body writes_exact_ready_plan_sixth 0 "Wrote reviewed plan" "$READY_PLUGIN_ROOT" "$BASE_PROTECTED_BODY" --plan --out "$PLAN_PATH_READY"
run_apply_case_with_protected_body rejects_protected_tamper 1 "Protected project fingerprint mismatch" "$READY_PLUGIN_ROOT" "$PRIVATE_PROTECTED_A"$'\n'"replacement-prod-13579"$'\n' --apply --plan-file "$PLAN_PATH_READY"

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s apply assertions failed\n' "$FAILURES" >&2
  exit 1
fi

printf 'PASS test_apply.sh\n'
