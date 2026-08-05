#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REVIEW_SCRIPT="$SCRIPT_DIR/review_config.sh"
PREFLIGHT_SCRIPT="$SCRIPT_DIR/preflight.sh"
AUDIT_SCRIPT="$SCRIPT_DIR/audit_iam.sh"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

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

GCLOUD_LOG="$TMP_DIR/gcloud.log"
STUB_BIN="$TMP_DIR/bin"
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
      *)
        printf 'unexpected run service status\n' >&2
        exit 99
        ;;
    esac
    ;;
  run\ services\ describe\ *"--format=value(spec.template.spec.serviceAccountName)"*)
    printf '%s\n' "${GCLOUD_RUN_SERVICE_ACCOUNT:-}"
    ;;
  run\ services\ describe\ *"--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/minScale)"*)
    printf '%s\n' "${GCLOUD_MIN_SCALE:-0}"
    ;;
  run\ services\ describe\ *"--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/maxScale)"*)
    printf '%s\n' "${GCLOUD_MAX_SCALE:-1}"
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
  artifacts\ repositories\ get-iam-policy\ mim\ *"--format=json"*)
    case "${GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: repository mim missing\n' >&2; exit 1 ;;
      error) printf 'permission denied\n' >&2; exit 1 ;;
    esac
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
  secrets\ get-iam-policy\ mim-app-gateway-origin-v1\ *"--format=json"*)
    case "${GCLOUD_APP_GATEWAY_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_APP_GATEWAY_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-app-gateway-origin-v1 missing\n' >&2; exit 1 ;;
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
  secrets\ get-iam-policy\ mim-edge-origin-v1\ *"--format=json"*)
    case "${GCLOUD_EDGE_ORIGIN_SECRET_IAM_STATUS:-exists}" in
      exists) printf '%s\n' "${GCLOUD_EDGE_ORIGIN_SECRET_IAM_JSON:-{\"bindings\":[]}}" ;;
      missing) printf 'NOT_FOUND: secret mim-edge-origin-v1 missing\n' >&2; exit 1 ;;
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

[[ "${CLOUDSDK_CORE_ACCOUNT:-}" == "operator.test@madup.com" ]] || {
  printf 'missing explicit bq account context: %s\n' "${CLOUDSDK_CORE_ACCOUNT:-}" >&2
  exit 98
}
[[ "${CLOUDSDK_CORE_PROJECT:-}" == "mim-prod-123456" ]] || {
  printf 'missing explicit bq project context: %s\n' "${CLOUDSDK_CORE_PROJECT:-}" >&2
  exit 98
}
printf 'bq account=%s project=%s %s\n' "${CLOUDSDK_CORE_ACCOUNT:?}" "${CLOUDSDK_CORE_PROJECT:?}" "$*" >> "${GCLOUD_LOG:?}"

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
readonly CENTRAL_IAP_SERVICE_AGENT="service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com"
readonly FOREIGN_IAP_SERVICE_AGENT='service-111111111111@gcp-sa-iap.iam.gserviceaccount.com'

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

project_policy_with_extra_secret_admin_member() {
  DEFAULT_BASE_PROJECT_POLICY="$(default_project_iam_policy)" EXTRA_MEMBER="serviceAccount:$DEPLOY_WORKER_EMAIL" python3 - <<'PY'
import json
import os

data = json.loads(os.environ["DEFAULT_BASE_PROJECT_POLICY"])
for binding in data.get("bindings", []):
    if binding.get("role") != "roles/secretmanager.admin":
        continue
    members = list(binding.get("members", []))
    members.append(os.environ["EXTRA_MEMBER"])
    binding["members"] = members
    break
print(json.dumps(data, separators=(",", ":")))
PY
}

project_policy_with_extra_member() {
  local role=$1
  local member=$2
  DEFAULT_BASE_PROJECT_POLICY="$(default_project_iam_policy)" EXTRA_ROLE="$role" EXTRA_MEMBER="$member" python3 - <<'PY'
import json
import os

data = json.loads(os.environ["DEFAULT_BASE_PROJECT_POLICY"])
data.setdefault("bindings", []).append(
    {"role": os.environ["EXTRA_ROLE"], "members": [os.environ["EXTRA_MEMBER"]]}
)
print(json.dumps(data, separators=(",", ":")))
PY
}

default_bq_dataset_json() {
  cat <<EOF
{"datasetReference":{"projectId":"$PROJECT_ID","datasetId":"mim_billing_export"},"access":[]}
EOF
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

assert_scoped_gcloud_log() {
  local case_name=$1
  while IFS= read -r line; do
    [[ "$line" == bq\ * ]] && continue
    [[ "$line" == *"--account=$OPERATOR_EMAIL"* ]] || {
      printf 'FAIL %s: gcloud call missing --account\n' "$case_name" >&2
      printf '%s\n' "$line" >&2
      FAILURES=$((FAILURES + 1))
    }
    case "$line" in
      auth\ list\ *)
        :
        ;;
      *)
        [[ "$line" == *"--project=$PROJECT_ID"* ]] || {
          printf 'FAIL %s: gcloud call missing --project\n' "$case_name" >&2
          printf '%s\n' "$line" >&2
          FAILURES=$((FAILURES + 1))
        }
        ;;
    esac
  done <"$GCLOUD_LOG"
}

assert_scoped_bq_log() {
  local case_name=$1
  local saw_bq=0
  while IFS= read -r line; do
    [[ "$line" == bq\ * ]] || continue
    saw_bq=1
    [[ "$line" == *"account=$OPERATOR_EMAIL"* ]] || {
      printf 'FAIL %s: bq call missing account context\n' "$case_name" >&2
      printf '%s\n' "$line" >&2
      FAILURES=$((FAILURES + 1))
    }
    [[ "$line" == *"project=$PROJECT_ID"* ]] || {
      printf 'FAIL %s: bq call missing project context\n' "$case_name" >&2
      printf '%s\n' "$line" >&2
      FAILURES=$((FAILURES + 1))
    }
  done <"$GCLOUD_LOG"
  [[ "$saw_bq" -eq 1 ]] || {
    printf 'FAIL %s: expected at least one bq call\n' "$case_name" >&2
    FAILURES=$((FAILURES + 1))
  }
}

run_case() {
  local case_name=$1
  local script_path=$2
  local expected_exit=$3
  local expected_substring=$4
  shift 4

  local output_path="$TMP_DIR/$case_name.out"
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  write_valid_config "$config_path"
  write_protected_file "$protected_path"
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
    GCLOUD_PROJECT_IAM_POLICY_JSON="${GCLOUD_PROJECT_IAM_POLICY_JSON:-$(default_project_iam_policy)}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON="${GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON:-$(default_artifact_repo_policy)}" \
    GCLOUD_BUILD_SA_POLICY_JSON="${GCLOUD_BUILD_SA_POLICY_JSON:-$(default_build_sa_policy)}" \
    GCLOUD_BUILD_SA_POLICY_STATUS="${GCLOUD_BUILD_SA_POLICY_STATUS:-exists}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_JSON="${GCLOUD_CONTROL_PLANE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS="${GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_JSON="${GCLOUD_APP_GATEWAY_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_STATUS="${GCLOUD_APP_GATEWAY_SA_POLICY_STATUS:-exists}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON="${GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS="${GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS:-exists}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON="${GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON:-$(default_identity_sync_sa_policy)}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS="${GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS:-exists}" \
    GCLOUD_MAINTENANCE_SA_POLICY_JSON="${GCLOUD_MAINTENANCE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_MAINTENANCE_SA_POLICY_STATUS="${GCLOUD_MAINTENANCE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_RELEASE_SA_POLICY_JSON="${GCLOUD_RELEASE_SA_POLICY_JSON:-$(default_release_sa_policy)}" \
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
    GCLOUD_ARTIFACT_REPOSITORY_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_STATUS:-missing}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS:-exists}" \
    GCLOUD_FIRESTORE_STATUS="${GCLOUD_FIRESTORE_STATUS:-missing}" \
    GCLOUD_FIRESTORE_LOCATION="${GCLOUD_FIRESTORE_LOCATION:-asia-northeast3}" \
    GCLOUD_FIRESTORE_TYPE="${GCLOUD_FIRESTORE_TYPE:-FIRESTORE_NATIVE}" \
    GCLOUD_QUEUE_STATUS="${GCLOUD_QUEUE_STATUS:-missing}" \
    GCLOUD_SECRET_STATUS="${GCLOUD_SECRET_STATUS:-missing}" \
    BQ_DATASET_JSON="${BQ_DATASET_JSON:-$(default_bq_dataset_json)}" \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    bash "$script_path" "$@" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  assert_contains "$output_path" "$expected_substring" "$case_name"
  assert_not_contains "$output_path" "$CLOUDFLARE_ACCOUNT_ID" "$case_name"
  assert_not_contains "$output_path" "$SLACK_APPROVED_WORKSPACE_IDS" "$case_name"
  printf 'PASS %s\n' "$case_name"
}

run_case_with_protected_body() {
  local case_name=$1
  local script_path=$2
  local expected_exit=$3
  local expected_substring=$4
  local protected_body=$5
  shift 5

  local output_path="$TMP_DIR/$case_name.out"
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  write_valid_config "$config_path"
  write_protected_file "$protected_path" "$protected_body"
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
    GCLOUD_PROJECT_IAM_POLICY_JSON="${GCLOUD_PROJECT_IAM_POLICY_JSON:-$(default_project_iam_policy)}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON="${GCLOUD_ARTIFACT_REPOSITORY_IAM_JSON:-$(default_artifact_repo_policy)}" \
    GCLOUD_BUILD_SA_POLICY_JSON="${GCLOUD_BUILD_SA_POLICY_JSON:-$(default_build_sa_policy)}" \
    GCLOUD_BUILD_SA_POLICY_STATUS="${GCLOUD_BUILD_SA_POLICY_STATUS:-exists}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_JSON="${GCLOUD_CONTROL_PLANE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS="${GCLOUD_CONTROL_PLANE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_JSON="${GCLOUD_APP_GATEWAY_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_APP_GATEWAY_SA_POLICY_STATUS="${GCLOUD_APP_GATEWAY_SA_POLICY_STATUS:-exists}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON="${GCLOUD_DEPLOY_WORKER_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS="${GCLOUD_DEPLOY_WORKER_SA_POLICY_STATUS:-exists}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON="${GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON:-$(default_identity_sync_sa_policy)}" \
    GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS="${GCLOUD_IDENTITY_SYNC_SA_POLICY_STATUS:-exists}" \
    GCLOUD_MAINTENANCE_SA_POLICY_JSON="${GCLOUD_MAINTENANCE_SA_POLICY_JSON:-$(default_release_act_as_policy)}" \
    GCLOUD_MAINTENANCE_SA_POLICY_STATUS="${GCLOUD_MAINTENANCE_SA_POLICY_STATUS:-exists}" \
    GCLOUD_RELEASE_SA_POLICY_JSON="${GCLOUD_RELEASE_SA_POLICY_JSON:-$(default_release_sa_policy)}" \
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
    GCLOUD_ARTIFACT_REPOSITORY_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_STATUS:-missing}" \
    GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS="${GCLOUD_ARTIFACT_REPOSITORY_IAM_STATUS:-exists}" \
    GCLOUD_FIRESTORE_STATUS="${GCLOUD_FIRESTORE_STATUS:-missing}" \
    GCLOUD_FIRESTORE_LOCATION="${GCLOUD_FIRESTORE_LOCATION:-asia-northeast3}" \
    GCLOUD_FIRESTORE_TYPE="${GCLOUD_FIRESTORE_TYPE:-FIRESTORE_NATIVE}" \
    GCLOUD_QUEUE_STATUS="${GCLOUD_QUEUE_STATUS:-missing}" \
    GCLOUD_SECRET_STATUS="${GCLOUD_SECRET_STATUS:-missing}" \
    BQ_DATASET_JSON="${BQ_DATASET_JSON:-$(default_bq_dataset_json)}" \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    bash "$script_path" "$@" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  assert_contains "$output_path" "$expected_substring" "$case_name"
  printf 'PASS %s\n' "$case_name"
}

run_case review_succeeds "$REVIEW_SCRIPT" 0 "Configuration review passed."

run_case preflight_allows_missing_bootstrap_resources "$PREFLIGHT_SCRIPT" 0 "Preflight checks passed."
assert_scoped_gcloud_log preflight_allows_missing_bootstrap_resources
assert_scoped_bq_log preflight_allows_missing_bootstrap_resources

BQ_DATASET_STATUS=missing \
run_case preflight_allows_missing_raw_billing_export_dataset "$PREFLIGHT_SCRIPT" 0 "Preflight checks passed."

GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_RUN_SERVICE_ACCOUNT="mim-runtime@$PROJECT_ID.iam.gserviceaccount.com" \
run_case preflight_rejects_shared_runtime_identity "$PREFLIGHT_SCRIPT" 1 "Cloud Run service must use the dedicated control-plane identity"

run_case_with_protected_body preflight_rejects_protected_project "$PREFLIGHT_SCRIPT" 1 "Selected project is protected" "$PROJECT_ID"$'\n'

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[{"role":"roles/editor","members":["serviceAccount:mim-control-plane@mim-prod-123456.iam.gserviceaccount.com"]}]}' \
run_case audit_rejects_control_plane_project_role "$AUDIT_SCRIPT" 1 "Managed identity mim-control-plane must not hold project role roles/editor"

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[{"role":"roles/datastore.user","members":["serviceAccount:mim-identity-sync@mim-prod-123456.iam.gserviceaccount.com"]}]}' \
run_case audit_rejects_identity_sync_project_role "$AUDIT_SCRIPT" 1 "Managed identity mim-identity-sync must not hold project role roles/datastore.user"

GCLOUD_IDENTITY_SYNC_SA_POLICY_JSON='{"bindings":[]}' \
run_case audit_rejects_missing_maintenance_token_creator "$AUDIT_SCRIPT" 1 "Managed identity mim-identity-sync must grant roles/iam.serviceAccountTokenCreator to serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"

GCLOUD_CONTROL_PLANE_SA_POLICY_JSON='{"bindings":[]}' \
run_case audit_rejects_missing_release_control_plane_act_as "$AUDIT_SCRIPT" 1 "Managed identity mim-control-plane must grant roles/iam.serviceAccountUser to serviceAccount:mim-release@mim-prod-123456.iam.gserviceaccount.com"

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[{"role":"roles/cloudscheduler.admin","members":["serviceAccount:mim-control-plane@mim-prod-123456.iam.gserviceaccount.com"],"condition":{"title":"temporary","expression":"true"}}]}' \
run_case audit_rejects_conditioned_control_plane_scheduler_admin "$AUDIT_SCRIPT" 1 "Managed identity mim-control-plane must not hold project role roles/cloudscheduler.admin"

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[{"role":"roles/iam.serviceAccountAdmin","members":["serviceAccount:mim-deploy-worker@mim-prod-123456.iam.gserviceaccount.com"]}]}' \
run_case audit_rejects_unconditioned_deploy_worker_service_account_admin "$AUDIT_SCRIPT" 1 "Managed identity mim-deploy-worker must not hold project role roles/iam.serviceAccountAdmin"

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[{"role":"roles/iam.serviceAccountAdmin","members":["serviceAccount:mim-deploy-worker@mim-prod-123456.iam.gserviceaccount.com"],"condition":{"title":"mim-workload-service-accounts","expression":"resource.name.startsWith(\"projects/mim-prod-123456/serviceAccounts/mim-wrk-\")"}}]}' \
run_case audit_rejects_deploy_worker_service_account_admin_without_resource_type "$AUDIT_SCRIPT" 1 "Managed identity mim-deploy-worker must not hold project role roles/iam.serviceAccountAdmin"

GCLOUD_PROJECT_IAM_POLICY_JSON="$(project_policy_with_extra_secret_admin_member)" \
run_case audit_rejects_deploy_worker_project_secretmanager_admin "$AUDIT_SCRIPT" 1 "Managed identity mim-deploy-worker must not hold project role roles/secretmanager.admin"

GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_STATUS=missing \
run_case audit_allows_missing_optional_previous_app_gateway_secret "$AUDIT_SCRIPT" 0 "IAM boundary audit passed."

GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_STATUS=exists \
GCLOUD_APP_GATEWAY_PREVIOUS_SECRET_IAM_JSON='{"bindings":[{"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:'"$APP_GATEWAY_EMAIL"'","serviceAccount:'"$CONTROL_PLANE_EMAIL"'"]}]}' \
run_case audit_rejects_optional_previous_app_gateway_secret_member_drift "$AUDIT_SCRIPT" 1 "Secret resource projects/mim-prod-123456/secrets/mim-app-gateway-origin-v0 binding roles/secretmanager.secretAccessor members drifted"

GCLOUD_GITHUB_APP_KEY_SECRET_IAM_JSON='{"bindings":[{"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:'"$CONTROL_PLANE_EMAIL"'"]}]}' \
run_case audit_rejects_github_app_key_secret_member_drift "$AUDIT_SCRIPT" 1 "Secret resource projects/mim-prod-123456/secrets/mim-github-app-key binding roles/secretmanager.secretAccessor members drifted"

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[{"role":"roles/iap.httpsResourceAccessor","members":["user:operator.test@madup.com"]}]}' \
run_case audit_rejects_project_wide_iap_accessor "$AUDIT_SCRIPT" 1 "Project-wide IAP access bindings are forbidden"

GCLOUD_PROJECT_IAM_POLICY_JSON='{"bindings":[{"role":"roles/datastore.user","members":["serviceAccount:foreign@other-project.iam.gserviceaccount.com"]}]}' \
run_case audit_rejects_cross_project_service_account "$AUDIT_SCRIPT" 1 "Cross-project service account bindings are forbidden"

GCLOUD_PROJECT_IAM_POLICY_JSON="$(project_policy_with_extra_member "roles/logging.logWriter" "serviceAccount:$FOREIGN_IAP_SERVICE_AGENT")" \
run_case audit_rejects_foreign_google_service_agent "$AUDIT_SCRIPT" 1 "Cross-project service account bindings are forbidden"

GCLOUD_PROJECT_IAM_POLICY_JSON="$(project_policy_with_extra_member "roles/logging.logWriter" "serviceAccount:$CENTRAL_IAP_SERVICE_AGENT")" \
run_case audit_allows_central_google_service_agent "$AUDIT_SCRIPT" 0 "IAM boundary audit passed."

BQ_DATASET_STATUS=error \
run_case audit_fails_closed_on_raw_billing_export_read_error "$AUDIT_SCRIPT" 1 "Unable to inspect raw billing export dataset"

BQ_DATASET_STATUS=misleading_not_found \
run_case audit_fails_closed_on_misleading_not_found_dataset_error "$AUDIT_SCRIPT" 1 "Unable to inspect raw billing export dataset"

GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS=error \
run_case audit_fails_closed_on_bootstrap_secret_policy_error "$AUDIT_SCRIPT" 1 "Unable to inspect Secret Manager IAM policy for secret mim-runtime-bootstrap"

GCLOUD_BOOTSTRAP_SECRET_IAM_STATUS=misleading_not_found \
run_case audit_fails_closed_on_misleading_secret_not_found "$AUDIT_SCRIPT" 1 "Unable to inspect Secret Manager IAM policy for secret mim-runtime-bootstrap"

GCLOUD_GITHUB_WEBHOOK_SECRET_IAM_STATUS=error \
run_case audit_fails_closed_on_github_webhook_secret_policy_error "$AUDIT_SCRIPT" 1 "Unable to inspect Secret Manager IAM policy for secret mim-github-webhook"

GCLOUD_RUN_SERVICE_STATUS=exists \
GCLOUD_RUN_SERVICE_ACCOUNT="$CONTROL_PLANE_EMAIL" \
GCLOUD_MIN_SCALE=2 \
run_case audit_rejects_wrong_min_scale "$AUDIT_SCRIPT" 1 "Cloud Run service minimum instances must be 0"

run_case audit_succeeds_with_missing_resources "$AUDIT_SCRIPT" 0 "IAM boundary audit passed."
assert_scoped_gcloud_log audit_succeeds_with_missing_resources
assert_scoped_bq_log audit_succeeds_with_missing_resources

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s preflight assertions failed\n' "$FAILURES" >&2
  exit 1
fi

printf 'PASS test_preflight.sh\n'
