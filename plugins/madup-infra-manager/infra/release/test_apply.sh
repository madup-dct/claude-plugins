#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
APPLY_SCRIPT="$SCRIPT_DIR/apply.sh"
TEST_LIB="$SCRIPT_DIR/test_task18_lib.sh"
. "$TEST_LIB"
. "$SCRIPT_DIR/task18_lib.sh"

TMP_DIR=$(mktemp -d)
STATE_TOKEN=$$
STATE_DIR="$TMP_DIR/state"
STUB_BIN="$TMP_DIR/bin"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
CURL_LOG="$TMP_DIR/curl.log"
SLACK_CAPTURE_DIR="$TMP_DIR/slack"

mkdir -p "$STATE_DIR" "$STUB_BIN" "$SLACK_CAPTURE_DIR" "$SCRIPT_DIR/.state"

JOB_IDENTITY_SYNC=mim-identity-sync
JOB_LIFECYCLE=mim-lifecycle
JOB_USAGE_INGEST=mim-usage-ingest
RELEASE_EMAIL="mim-release@$TASK18_PROJECT_ID.iam.gserviceaccount.com"
MAINTENANCE_EMAIL="mim-maintenance@$TASK18_PROJECT_ID.iam.gserviceaccount.com"
PLAN_PATH="$SCRIPT_DIR/.state/test-release-apply-$STATE_TOKEN.json"
NOOP_PLAN_PATH="$SCRIPT_DIR/.state/test-release-apply-noop-$STATE_TOKEN.json"
GOOGLE_ONLY_PLAN_PATH="$SCRIPT_DIR/.state/test-release-apply-google-only-$STATE_TOKEN.json"
NO_PREVIOUS_PROOF_PLAN_PATH="$SCRIPT_DIR/.state/test-release-apply-no-previous-proof-$STATE_TOKEN.json"
TENANT_EVIDENCE_PATH="$SCRIPT_DIR/.state/test-tenant-evidence-apply-$STATE_TOKEN.json"
NOW_EPOCH=$(date +%s)
FAILURES=0

export TASK18_PROJECT_ID TASK18_REVIEWED_RUNTIME_IMAGE_URI TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI
export TASK18_APP_CLOUDFLARE_ACCESS_ISSUER TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE
export TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID TASK18_APP_GATEWAY_PROOF_SECRET_VERSION
export TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION
export MAINTENANCE_EMAIL MIM_TASK18_FIXED_REGION
export RELEASE_EMAIL

trap 'rm -rf "$TMP_DIR"; rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" "$NOOP_PLAN_PATH" "$NOOP_PLAN_PATH.sha256" "$GOOGLE_ONLY_PLAN_PATH" "$GOOGLE_ONLY_PLAN_PATH.sha256" "$NO_PREVIOUS_PROOF_PLAN_PATH" "$NO_PREVIOUS_PROOF_PLAN_PATH.sha256" "$TENANT_EVIDENCE_PATH" "$TENANT_EVIDENCE_PATH.sha256" 2>/dev/null || true' EXIT

cat >"$STUB_BIN/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-C" && "${3:-}" == "rev-parse" && "${4:-}" == "HEAD" ]]; then
  printf '%s\n' "${TASK18_SOURCE_COMMIT:?}"
  exit 0
fi
printf 'unexpected git invocation: %s\n' "$*" >&2
exit 99
EOF
chmod +x "$STUB_BIN/git"

cat >"$STUB_BIN/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"

task18_job_module() {
  case "$1" in
    mim-identity-sync) printf 'mim_control_plane.jobs.directory_sync' ;;
    mim-lifecycle) printf 'mim_control_plane.jobs.lifecycle' ;;
    mim-usage-ingest) printf 'mim_control_plane.jobs.usage_ingest' ;;
    *) return 1 ;;
  esac
}

task18_job_runtime_mode() {
  case "$1" in
    mim-identity-sync) printf 'identity-sync' ;;
    mim-lifecycle) printf 'lifecycle' ;;
    mim-usage-ingest) printf 'usage-ingest' ;;
    *) return 1 ;;
  esac
}

task18_job_schedule() {
  case "$1" in
    mim-identity-sync) printf '*/15 * * * *' ;;
    mim-lifecycle) printf '7,22,37,52 * * * *' ;;
    mim-usage-ingest) printf '12 * * * *' ;;
    *) return 1 ;;
  esac
}

task18_job_uri() {
  printf 'https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run' \
    "${TASK18_PROJECT_ID:?}" "${MIM_TASK18_FIXED_REGION:?}" "$1"
}

write_service_json() {
  local path=$1
  local service_name=$2
  local service_account=$3
  local image_uri=$4
  local runtime_mode=$5
  local ingress=$6
  python3 - "$path" "$service_name" "$service_account" "$image_uri" "$runtime_mode" "$ingress" "${TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION:?}" <<'PY'
import json
import sys
from pathlib import Path

path, service_name, service_account, image_uri, runtime_mode, ingress, bootstrap = sys.argv[1:]
payload = {
    "metadata": {
        "name": service_name,
        "annotations": {
            "run.googleapis.com/minScale": "0",
            "run.googleapis.com/maxScale": "1",
        },
    },
    "spec": {
        "template": {
            "metadata": {
                "annotations": {
                    "autoscaling.knative.dev/minScale": "0",
                    "autoscaling.knative.dev/maxScale": "1",
                    "run.googleapis.com/cpu-throttling": "true",
                    "run.googleapis.com/startup-cpu-boost": "false",
                    "run.googleapis.com/ingress": ingress,
                }
            },
            "spec": {
                "serviceAccountName": service_account,
                "containerConcurrency": 20,
                "timeoutSeconds": 300,
                "containers": [
                    {
                        "image": image_uri,
                        "env": [
                            {"name": "MIM_RUNTIME_MODE", "value": runtime_mode},
                            {"name": "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION", "value": bootstrap},
                            {"name": "MIM_ENABLE_MUTATIONS", "value": "true"},
                        ],
                        "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                    }
                ],
            },
        }
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_iam_json() {
  local path=$1
  shift
  python3 - "$path" "$@" <<'PY'
import json
import sys
from pathlib import Path

members = sys.argv[2:]
payload = {"bindings": [{"role": "roles/run.invoker", "members": members}] if members else []}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_job_json() {
  local path=$1
  local job_name=$2
  local runtime_mode module
  runtime_mode=$(task18_job_runtime_mode "$job_name")
  module=$(task18_job_module "$job_name")
  python3 - "$path" "$job_name" "$runtime_mode" "$module" "${TASK18_REVIEWED_RUNTIME_IMAGE_URI:?}" "${TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION:?}" "${MAINTENANCE_EMAIL:?}" "${TASK18_PROJECT_ID:?}" "${MIM_TASK18_FIXED_REGION:?}" <<'PY'
import json
import sys
from pathlib import Path

path, job_name, runtime_mode, module, image_uri, bootstrap, service_account, project_id, region = sys.argv[1:]
payload = {
    "name": f"projects/{project_id}/locations/{region}/jobs/{job_name}",
    "template": {
        "taskCount": 1,
        "parallelism": 1,
        "template": {
            "maxRetries": 0,
            "timeout": "600s",
            "serviceAccount": service_account,
            "containers": [
                {
                    "image": image_uri,
                    "command": ["python"],
                    "args": ["-m", module],
                    "env": [
                        {"name": "MIM_RUNTIME_MODE", "value": runtime_mode},
                        {"name": "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION", "value": bootstrap},
                        {"name": "MIM_ENABLE_MUTATIONS", "value": "true"},
                    ],
                    "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                }
            ],
        },
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_scheduler_json() {
  local path=$1
  local job_name=$2
  local schedule
  schedule=$(task18_job_schedule "$job_name")
  python3 - "$path" "$job_name" "$schedule" "$(task18_job_uri "$job_name")" "${MAINTENANCE_EMAIL:?}" "${TASK18_PROJECT_ID:?}" "${MIM_TASK18_FIXED_REGION:?}" <<'PY'
import json
import sys
from pathlib import Path

path, job_name, schedule, uri, service_account, project_id, region = sys.argv[1:]
payload = {
    "name": f"projects/{project_id}/locations/{region}/jobs/{job_name}",
    "schedule": schedule,
    "timeZone": "UTC",
    "state": "ENABLED",
    "httpTarget": {
        "httpMethod": "POST",
        "uri": uri,
        "oauthToken": {"serviceAccountEmail": service_account},
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_service_json() {
  local path=$1
  local service_name=$2
  local service_account=$3
  local image_uri=$4
  local runtime_mode=$5
  local ingress=$6
  python3 - "$path" "$service_name" "$service_account" "$image_uri" "$runtime_mode" "$ingress" "$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" "$TASK18_PROJECT_ID" "$TASK18_APP_CLOUDFLARE_ACCESS_ISSUER" "$TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE" "$TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID" "$TASK18_APP_GATEWAY_PROOF_SECRET_VERSION" "$TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID" "$TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION" <<'PY'
import json
import sys
from pathlib import Path

path, service_name, service_account, image_uri, runtime_mode, ingress, bootstrap, project_id, app_issuer, app_audience, current_key_id, current_secret, previous_key_id, previous_secret = sys.argv[1:]
project_number = "987654321012"
env = [
    {"name": "MIM_RUNTIME_MODE", "value": runtime_mode},
    {"name": "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION", "value": bootstrap},
    {"name": "MIM_ENABLE_MUTATIONS", "value": "true"},
]
if service_name == "mim-app-gateway":
    env = [
        {"name": "MIM_PUBLIC_SUFFIX", "value": "madup.app"},
        {"name": "MIM_PROJECT_ID", "value": project_id},
        {"name": "MIM_PROJECT_NUMBER", "value": project_number},
        {"name": "MIM_REGION", "value": "asia-northeast3"},
        {"name": "MIM_CLOUDFLARE_ACCESS_ISSUER", "value": app_issuer},
        {"name": "MIM_CLOUDFLARE_ACCESS_AUDIENCE", "value": app_audience},
        {"name": "MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL", "value": service_account},
        {"name": "MIM_APP_AUTHORIZATION_URL", "value": f"https://mim-schedule-gateway-{project_number}.asia-northeast3.run.app/v1/apps/authorize"},
        {"name": "MIM_APP_AUTHORIZATION_AUDIENCE", "value": f"https://mim-schedule-gateway-{project_number}.asia-northeast3.run.app"},
        {"name": "MIM_APP_PROOF_CURRENT_KEY_ID", "value": current_key_id},
        {"name": "MIM_APP_PROOF_CURRENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "mim-app-gateway-origin-v1", "key": current_secret.rsplit('/', 1)[1]}}},
    ]
    if previous_key_id:
        env.append({"name": "MIM_APP_PROOF_PREVIOUS_KEY_ID", "value": previous_key_id})
    if previous_secret:
        env.append({"name": "MIM_APP_PROOF_PREVIOUS_SECRET", "valueFrom": {"secretKeyRef": {"name": "mim-app-gateway-origin-v0", "key": previous_secret.rsplit('/', 1)[1]}}})
payload = {
    "metadata": {
        "name": service_name,
        "annotations": {
            "run.googleapis.com/minScale": "0",
            "run.googleapis.com/maxScale": "1",
        },
    },
    "spec": {
        "template": {
            "metadata": {
                "annotations": {
                    "autoscaling.knative.dev/minScale": "0",
                    "autoscaling.knative.dev/maxScale": "1",
                    "run.googleapis.com/cpu-throttling": "true",
                    "run.googleapis.com/startup-cpu-boost": "false",
                    "run.googleapis.com/ingress": ingress,
                }
            },
            "spec": {
                "serviceAccountName": service_account,
                "containerConcurrency": 20,
                "timeoutSeconds": 300,
                "containers": [
                    {
                        "image": image_uri,
                        "env": env,
                        "resources": {
                            "limits": {
                                "cpu": "1",
                                "memory": "512Mi",
                            }
                        },
                    }
                ],
            },
        }
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_iam_json() {
  local path=$1
  shift
  python3 - "$path" "$@" <<'PY'
import json
import sys
from pathlib import Path

members = sys.argv[2:]
payload = {"bindings": [{"role": "roles/run.invoker", "members": members}] if members else []}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

case "$*" in
  auth\ list\ *"--format=value(account)"*)
    printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-operator.test@madup.com}"
    ;;
  projects\ describe\ mim-prod-123456\ *"--format=value(projectId)"*)
    printf 'mim-prod-123456\n'
    ;;
  projects\ describe\ mim-prod-123456\ *"--format=value(parent.type)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_TYPE:-organization}"
    ;;
  projects\ describe\ mim-prod-123456\ *"--format=value(parent.id)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_ID:-123456789012}"
    ;;
  projects\ describe\ mim-prod-123456\ *"--format=value(projectNumber)"*)
    printf '%s\n' "${GCLOUD_PROJECT_NUMBER:-987654321012}"
    ;;
  billing\ projects\ describe\ mim-prod-123456\ *"--format=value(billingEnabled)"*)
    printf '%s\n' "${GCLOUD_BILLING_ENABLED:-True}"
    ;;
  billing\ projects\ describe\ mim-prod-123456\ *"--format=value(billingAccountName)"*)
    printf '%s\n' "${GCLOUD_BILLING_ACCOUNT_NAME:-billingAccounts/ABCDEF-123456-7890AB}"
    ;;
  iam\ service-accounts\ describe\ mim-release@mim-prod-123456.iam.gserviceaccount.com\ *"--format=value(email)"*)
    printf 'mim-release@mim-prod-123456.iam.gserviceaccount.com\n'
    ;;
  iam\ service-accounts\ describe\ mim-maintenance@mim-prod-123456.iam.gserviceaccount.com\ *"--format=value(email)"*)
    printf 'mim-maintenance@mim-prod-123456.iam.gserviceaccount.com\n'
    ;;
  builds\ describe\ builder-build-123456\ *"--format=json"*)
    cat "${STATE_DIR:?}/builder-build.json"
    ;;
  builds\ describe\ app-gateway-build-222333\ *"--format=json"*)
    cat "${STATE_DIR:?}/app-gateway-build.json"
    ;;
  builds\ describe\ runtime-build-654321\ *"--format=json"*)
    cat "${STATE_DIR:?}/runtime-build.json"
    ;;
  artifacts\ docker\ images\ describe\ asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-platform/mim-builder@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\ *"--format=json"*)
    cat "${STATE_DIR:?}/builder-artifact.json"
    ;;
  artifacts\ docker\ images\ describe\ asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-platform/app-gateway@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\ *"--format=json"*)
    cat "${STATE_DIR:?}/app-gateway-artifact.json"
    ;;
  artifacts\ docker\ images\ describe\ asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-control-plane/runtime@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\ *"--format=json"*)
    cat "${STATE_DIR:?}/runtime-artifact.json"
    ;;
  secrets\ versions\ describe\ 7\ "--secret=mim-runtime-bootstrap"\ *"--format=json"*)
    cat "${STATE_DIR:?}/bootstrap-secret.json"
    ;;
  secrets\ versions\ describe\ 11\ "--secret=mim-app-gateway-origin-v1"\ *"--format=json"*)
    cat "${STATE_DIR:?}/app-gateway-proof-secret.json"
    ;;
  projects\ get-iam-policy\ mim-prod-123456\ *"--format=json"*)
    cat "${STATE_DIR:?}/project-iam.json"
    ;;
  projects\ get-iam-policy\ mim-prod-123456\ *"--filter=bindings.role=roles/run.invoker"*)
    printf '%s\n' "${GCLOUD_PROJECT_RUN_INVOKER_MEMBERS:-}"
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-release@mim-prod-123456.iam.gserviceaccount.com\ *"--format=json"*)
    cat "${STATE_DIR:?}/release-iam.json"
    ;;
  iam\ service-accounts\ get-iam-policy\ mim-maintenance@mim-prod-123456.iam.gserviceaccount.com\ *"--format=json"*)
    cat "${STATE_DIR:?}/maintenance-iam.json"
    ;;
  run\ services\ describe\ mim-control-plane\ *"--format=json"*)
    cat "${STATE_DIR:?}/mim-control-plane.service.json"
    ;;
  run\ services\ describe\ mim-app-gateway\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-app-gateway.service.json" ]]; then
      cat "${STATE_DIR:?}/mim-app-gateway.service.json"
    else
      printf 'NOT_FOUND: missing service\n' >&2
      exit 1
    fi
    ;;
  run\ services\ describe\ mim-deploy-worker\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-deploy-worker.service.json" ]]; then
      cat "${STATE_DIR:?}/mim-deploy-worker.service.json"
    else
      printf 'NOT_FOUND: missing service\n' >&2
      exit 1
    fi
    ;;
  run\ services\ describe\ mim-schedule-gateway\ *"--format=json"*)
    cat "${STATE_DIR:?}/mim-schedule-gateway.service.json"
    ;;
  run\ services\ get-iam-policy\ mim-control-plane\ *"--format=json"*)
    cat "${STATE_DIR:?}/mim-control-plane.iam.json"
    ;;
  run\ services\ get-iam-policy\ mim-app-gateway\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-app-gateway.iam.json" ]]; then
      cat "${STATE_DIR:?}/mim-app-gateway.iam.json"
    else
      printf 'NOT_FOUND: missing iam policy\n' >&2
      exit 1
    fi
    ;;
  run\ services\ get-iam-policy\ mim-deploy-worker\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-deploy-worker.iam.json" ]]; then
      cat "${STATE_DIR:?}/mim-deploy-worker.iam.json"
    else
      printf 'NOT_FOUND: missing iam policy\n' >&2
      exit 1
    fi
    ;;
  run\ services\ get-iam-policy\ mim-schedule-gateway\ *"--format=json"*)
    cat "${STATE_DIR:?}/mim-schedule-gateway.iam.json"
    ;;
  run\ jobs\ describe\ mim-identity-sync\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-identity-sync.job.json" ]]; then
      cat "${STATE_DIR:?}/mim-identity-sync.job.json"
    else
      printf 'NOT_FOUND: missing job\n' >&2
      exit 1
    fi
    ;;
  run\ jobs\ describe\ mim-lifecycle\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-lifecycle.job.json" ]]; then
      cat "${STATE_DIR:?}/mim-lifecycle.job.json"
    else
      printf 'NOT_FOUND: missing job\n' >&2
      exit 1
    fi
    ;;
  run\ jobs\ describe\ mim-usage-ingest\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-usage-ingest.job.json" ]]; then
      cat "${STATE_DIR:?}/mim-usage-ingest.job.json"
    else
      printf 'NOT_FOUND: missing job\n' >&2
      exit 1
    fi
    ;;
  scheduler\ jobs\ describe\ mim-identity-sync\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-identity-sync.scheduler.json" ]]; then
      cat "${STATE_DIR:?}/mim-identity-sync.scheduler.json"
    else
      printf 'NOT_FOUND: missing scheduler\n' >&2
      exit 1
    fi
    ;;
  scheduler\ jobs\ describe\ mim-lifecycle\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-lifecycle.scheduler.json" ]]; then
      cat "${STATE_DIR:?}/mim-lifecycle.scheduler.json"
    else
      printf 'NOT_FOUND: missing scheduler\n' >&2
      exit 1
    fi
    ;;
  scheduler\ jobs\ describe\ mim-usage-ingest\ *"--format=json"*)
    if [[ -f "${STATE_DIR:?}/mim-usage-ingest.scheduler.json" ]]; then
      cat "${STATE_DIR:?}/mim-usage-ingest.scheduler.json"
    else
      printf 'NOT_FOUND: missing scheduler\n' >&2
      exit 1
    fi
    ;;
  run\ jobs\ deploy\ mim-identity-sync*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_job_json "${STATE_DIR:?}/mim-identity-sync.job.json" "mim-identity-sync"
    fi
    ;;
  run\ jobs\ deploy\ mim-lifecycle*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_job_json "${STATE_DIR:?}/mim-lifecycle.job.json" "mim-lifecycle"
    fi
    ;;
  run\ jobs\ deploy\ mim-usage-ingest*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_job_json "${STATE_DIR:?}/mim-usage-ingest.job.json" "mim-usage-ingest"
    fi
    ;;
  scheduler\ jobs\ create\ http\ mim-identity-sync*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_scheduler_json "${STATE_DIR:?}/mim-identity-sync.scheduler.json" "mim-identity-sync"
    fi
    ;;
  scheduler\ jobs\ create\ http\ mim-lifecycle*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_scheduler_json "${STATE_DIR:?}/mim-lifecycle.scheduler.json" "mim-lifecycle"
    fi
    ;;
  scheduler\ jobs\ create\ http\ mim-usage-ingest*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_scheduler_json "${STATE_DIR:?}/mim-usage-ingest.scheduler.json" "mim-usage-ingest"
    fi
    ;;
  scheduler\ jobs\ update\ http\ mim-identity-sync*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_scheduler_json "${STATE_DIR:?}/mim-identity-sync.scheduler.json" "mim-identity-sync"
    fi
    ;;
  scheduler\ jobs\ update\ http\ mim-lifecycle*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_scheduler_json "${STATE_DIR:?}/mim-lifecycle.scheduler.json" "mim-lifecycle"
    fi
    ;;
  scheduler\ jobs\ update\ http\ mim-usage-ingest*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_scheduler_json "${STATE_DIR:?}/mim-usage-ingest.scheduler.json" "mim-usage-ingest"
    fi
    ;;
  run\ services\ deploy\ mim-control-plane*|run\ deploy\ mim-control-plane*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_service_json "${STATE_DIR:?}/mim-control-plane.service.json" \
        "mim-control-plane" \
        "mim-control-plane@${TASK18_PROJECT_ID:?}.iam.gserviceaccount.com" \
        "${TASK18_REVIEWED_RUNTIME_IMAGE_URI:?}" \
        "control-plane" \
        "all"
    fi
    ;;
  run\ services\ deploy\ mim-app-gateway*|run\ deploy\ mim-app-gateway*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_service_json "${STATE_DIR:?}/mim-app-gateway.service.json" \
        "mim-app-gateway" \
        "mim-app-gateway@${TASK18_PROJECT_ID:?}.iam.gserviceaccount.com" \
        "${TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI:?}" \
        "app-gateway" \
        "all"
    fi
    ;;
  run\ services\ deploy\ mim-deploy-worker*|run\ deploy\ mim-deploy-worker*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_service_json "${STATE_DIR:?}/mim-deploy-worker.service.json" \
        "mim-deploy-worker" \
        "mim-deploy-worker@${TASK18_PROJECT_ID:?}.iam.gserviceaccount.com" \
        "${TASK18_REVIEWED_RUNTIME_IMAGE_URI:?}" \
        "deploy-worker" \
        "internal"
    fi
    ;;
  run\ services\ deploy\ mim-schedule-gateway*|run\ deploy\ mim-schedule-gateway*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_service_json "${STATE_DIR:?}/mim-schedule-gateway.service.json" \
        "mim-schedule-gateway" \
        "mim-schedule-gateway@${TASK18_PROJECT_ID:?}.iam.gserviceaccount.com" \
        "${TASK18_REVIEWED_RUNTIME_IMAGE_URI:?}" \
        "schedule-gateway" \
        "internal"
    fi
    ;;
  run\ services\ add-iam-policy-binding\ mim-control-plane*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_iam_json "${STATE_DIR:?}/mim-control-plane.iam.json" "allUsers"
    fi
    ;;
  run\ services\ add-iam-policy-binding\ mim-app-gateway*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_iam_json "${STATE_DIR:?}/mim-app-gateway.iam.json" "allUsers"
    fi
    ;;
  run\ services\ add-iam-policy-binding\ mim-deploy-worker*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_iam_json "${STATE_DIR:?}/mim-deploy-worker.iam.json" "serviceAccount:mim-deploy-worker@${TASK18_PROJECT_ID:?}.iam.gserviceaccount.com"
    fi
    ;;
  run\ services\ add-iam-policy-binding\ mim-schedule-gateway*)
    if [[ "${GCLOUD_DISABLE_READBACK_UPDATE:-false}" != true ]]; then
      write_iam_json "${STATE_DIR:?}/mim-schedule-gateway.iam.json" "serviceAccount:${MAINTENANCE_EMAIL:?}" "serviceAccount:mim-app-gateway@${TASK18_PROJECT_ID:?}.iam.gserviceaccount.com"
    fi
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/gcloud"

cat >"$STUB_BIN/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${CURL_LOG:?}"
data_file=
prev=
for arg in "$@"; do
  case "$arg" in
    --data-binary=@*) data_file=${arg#--data-binary=@} ;;
    @*) [[ "$prev" == "--data-binary" ]] && data_file=${arg#@} ;;
  esac
  prev=$arg
done
[[ -n "$data_file" ]] || { printf 'missing data file\n' >&2; exit 99; }
cp "$data_file" "${SLACK_CAPTURE_DIR:?}/manifest-request.json"
cat <<'JSON'
{
  "ok": true,
  "manifest": {
    "oauth_config": {
      "redirect_urls": ["https://mim.madup.app/slack/oauth/callback"],
      "scopes": {
        "bot": ["chat:write", "commands"],
        "user": []
      }
    },
    "settings": {
      "org_deploy_enabled": true,
      "socket_mode_enabled": false
    }
  }
}
JSON
EOF
chmod +x "$STUB_BIN/curl"

write_service_json() {
  local path=$1
  local service_name=$2
  local service_account=$3
  local image_uri=$4
  local runtime_mode=$5
  local ingress=$6
  python3 - "$path" "$service_name" "$service_account" "$image_uri" "$runtime_mode" "$ingress" "$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" "$TASK18_PROJECT_ID" "$TASK18_APP_CLOUDFLARE_ACCESS_ISSUER" "$TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE" "$TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID" "$TASK18_APP_GATEWAY_PROOF_SECRET_VERSION" "$TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID" "$TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION" <<'PY'
import json
import sys
from pathlib import Path

path, service_name, service_account, image_uri, runtime_mode, ingress, bootstrap, project_id, app_issuer, app_audience, current_key_id, current_secret, previous_key_id, previous_secret = sys.argv[1:]
project_number = "987654321012"
env = [
    {"name": "MIM_RUNTIME_MODE", "value": runtime_mode},
    {"name": "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION", "value": bootstrap},
    {"name": "MIM_ENABLE_MUTATIONS", "value": "true"},
]
if service_name == "mim-app-gateway":
    env = [
        {"name": "MIM_PUBLIC_SUFFIX", "value": "madup.app"},
        {"name": "MIM_PROJECT_ID", "value": project_id},
        {"name": "MIM_PROJECT_NUMBER", "value": project_number},
        {"name": "MIM_REGION", "value": "asia-northeast3"},
        {"name": "MIM_CLOUDFLARE_ACCESS_ISSUER", "value": app_issuer},
        {"name": "MIM_CLOUDFLARE_ACCESS_AUDIENCE", "value": app_audience},
        {"name": "MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL", "value": service_account},
        {"name": "MIM_APP_AUTHORIZATION_URL", "value": f"https://mim-schedule-gateway-{project_number}.asia-northeast3.run.app/v1/apps/authorize"},
        {"name": "MIM_APP_AUTHORIZATION_AUDIENCE", "value": f"https://mim-schedule-gateway-{project_number}.asia-northeast3.run.app"},
        {"name": "MIM_APP_PROOF_CURRENT_KEY_ID", "value": current_key_id},
        {"name": "MIM_APP_PROOF_CURRENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "mim-app-gateway-origin-v1", "key": current_secret.rsplit('/', 1)[1]}}},
    ]
    if previous_key_id:
        env.append({"name": "MIM_APP_PROOF_PREVIOUS_KEY_ID", "value": previous_key_id})
    if previous_secret:
        env.append({"name": "MIM_APP_PROOF_PREVIOUS_SECRET", "valueFrom": {"secretKeyRef": {"name": "mim-app-gateway-origin-v0", "key": previous_secret.rsplit('/', 1)[1]}}})
payload = {
    "metadata": {
        "name": service_name,
        "annotations": {
            "run.googleapis.com/minScale": "0",
            "run.googleapis.com/maxScale": "1",
        },
    },
    "spec": {
        "template": {
            "metadata": {
                "annotations": {
                    "autoscaling.knative.dev/minScale": "0",
                    "autoscaling.knative.dev/maxScale": "1",
                    "run.googleapis.com/cpu-throttling": "true",
                    "run.googleapis.com/startup-cpu-boost": "false",
                    "run.googleapis.com/ingress": ingress,
                }
            },
            "spec": {
                "serviceAccountName": service_account,
                "containerConcurrency": 20,
                "timeoutSeconds": 300,
                "containers": [
                    {
                        "image": image_uri,
                        "env": env,
                        "resources": {
                            "limits": {
                                "cpu": "1",
                                "memory": "512Mi",
                            }
                        },
                    }
                ],
            },
        }
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_iam_json() {
  local path=$1
  shift
  python3 - "$path" "$@" <<'PY'
import json
import sys
from pathlib import Path

members = sys.argv[2:]
payload = {"bindings": [{"role": "roles/run.invoker", "members": members}] if members else []}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

task18_job_module() {
  case "$1" in
    "$JOB_IDENTITY_SYNC") printf 'mim_control_plane.jobs.directory_sync' ;;
    "$JOB_LIFECYCLE") printf 'mim_control_plane.jobs.lifecycle' ;;
    "$JOB_USAGE_INGEST") printf 'mim_control_plane.jobs.usage_ingest' ;;
    *) return 1 ;;
  esac
}

task18_job_schedule() {
  case "$1" in
    "$JOB_IDENTITY_SYNC") printf '*/15 * * * *' ;;
    "$JOB_LIFECYCLE") printf '7,22,37,52 * * * *' ;;
    "$JOB_USAGE_INGEST") printf '12 * * * *' ;;
    *) return 1 ;;
  esac
}

write_job_json() {
  local path=$1
  local job_name=$2
  python3 - "$path" "$job_name" "$(task18_job_module "$job_name")" "$TASK18_REVIEWED_RUNTIME_IMAGE_URI" "$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" "$MAINTENANCE_EMAIL" "$TASK18_PROJECT_ID" "$MIM_TASK18_FIXED_REGION" <<'PY'
import json
import sys
from pathlib import Path

path, job_name, module, image_uri, bootstrap, service_account, project_id, region = sys.argv[1:]
mode_map = {
    "mim-identity-sync": "identity-sync",
    "mim-lifecycle": "lifecycle",
    "mim-usage-ingest": "usage-ingest",
}
payload = {
    "name": f"projects/{project_id}/locations/{region}/jobs/{job_name}",
    "template": {
        "taskCount": 1,
        "parallelism": 1,
        "template": {
            "maxRetries": 0,
            "timeout": "600s",
            "serviceAccount": service_account,
            "containers": [
                {
                    "image": image_uri,
                    "command": ["python"],
                    "args": ["-m", module],
                    "env": [
                        {"name": "MIM_RUNTIME_MODE", "value": mode_map[job_name]},
                        {"name": "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION", "value": bootstrap},
                        {"name": "MIM_ENABLE_MUTATIONS", "value": "true"},
                    ],
                    "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                }
            ],
        },
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_scheduler_json() {
  local path=$1
  local job_name=$2
  local schedule uri
  schedule=$(task18_job_schedule "$job_name")
  uri="https://run.googleapis.com/v2/projects/$TASK18_PROJECT_ID/locations/$MIM_TASK18_FIXED_REGION/jobs/$job_name:run"
  python3 - "$path" "$job_name" "$schedule" "$uri" "$MAINTENANCE_EMAIL" "$TASK18_PROJECT_ID" "$MIM_TASK18_FIXED_REGION" <<'PY'
import json
import sys
from pathlib import Path

path, job_name, schedule, uri, service_account, project_id, region = sys.argv[1:]
payload = {
    "name": f"projects/{project_id}/locations/{region}/jobs/{job_name}",
    "schedule": schedule,
    "timeZone": "UTC",
    "state": "ENABLED",
    "httpTarget": {
        "httpMethod": "POST",
        "uri": uri,
        "oauthToken": {"serviceAccountEmail": service_account},
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_sa_policy_json() {
  local path=$1
  local role=$2
  local member=$3
  python3 - "$path" "$role" "$member" <<'PY'
import json
import sys
from pathlib import Path

payload = {"bindings": [{"role": sys.argv[2], "members": [sys.argv[3]]}]}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_project_policy_json() {
  python3 - "$STATE_DIR/project-iam.json" "$TASK18_PROJECT_ID" "$MIM_TASK18_FIXED_REGION" "$RELEASE_EMAIL" "$MAINTENANCE_EMAIL" <<'PY'
import json
import sys
from pathlib import Path

path, project_id, region, release_email, maintenance_email = sys.argv[1:]
release_member = f"serviceAccount:{release_email}"
maintenance_member = f"serviceAccount:{maintenance_email}"
job_expression = (
    f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-identity-sync" || '
    f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-lifecycle" || '
    f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-usage-ingest"'
)
payload = {
    "bindings": [
        {
            "role": "roles/run.admin",
            "members": [release_member],
            "condition": {
                "title": "mim-release-runtimes",
                "expression": (
                    f'resource.name == "projects/{project_id}/locations/{region}/services/mim-control-plane" || '
                    f'resource.name == "projects/{project_id}/locations/{region}/services/mim-app-gateway" || '
                    f'resource.name == "projects/{project_id}/locations/{region}/services/mim-deploy-worker" || '
                    f'resource.name == "projects/{project_id}/locations/{region}/services/mim-schedule-gateway" || '
                    + job_expression
                ),
            },
    },
    {
        "role": "roles/cloudscheduler.admin",
        "members": [release_member],
    },
    {
        "role": "roles/run.jobsExecutor",
        "members": [maintenance_member],
        "condition": {
            "title": "mim-fixed-maintenance-jobs",
            "expression": job_expression,
        },
    },
    ]
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

write_default_state() {
  python3 - "$STATE_DIR" "$TASK18_SOURCE_COMMIT" "$TASK18_REVIEWED_BUILDER_IMAGE_URI" "$TASK18_REVIEWED_RUNTIME_IMAGE_URI" "$TASK18_PROJECT_ID" <<'PY'
import json
import sys
from pathlib import Path

state_dir = Path(sys.argv[1])
source_commit = sys.argv[2]
builder_uri = sys.argv[3]
runtime_uri = sys.argv[4]
project_id = sys.argv[5]
build_sa = f"projects/{project_id}/serviceAccounts/mim-build@{project_id}.iam.gserviceaccount.com"
builder_tag = f"asia-northeast3-docker.pkg.dev/{project_id}/mim-platform/mim-builder:git-{source_commit}"
runtime_tag = f"asia-northeast3-docker.pkg.dev/{project_id}/mim-control-plane/runtime:git-{source_commit}"

(state_dir / "builder-build.json").write_text(json.dumps({
    "id": "builder-build-123456",
    "results": {"images": [{"digest": "sha256:" + "d" * 64, "name": builder_tag}]},
    "serviceAccount": build_sa,
    "status": "SUCCESS",
    "substitutions": {"_MIM_SOURCE_COMMIT": source_commit},
}, indent=2, sort_keys=True) + "\n")
(state_dir / "app-gateway-build.json").write_text(json.dumps({
    "id": "app-gateway-build-222333",
    "results": {"images": [{"digest": "sha256:" + "b" * 64, "name": f"asia-northeast3-docker.pkg.dev/{project_id}/mim-platform/app-gateway:git-{source_commit}"}]},
    "serviceAccount": build_sa,
    "status": "SUCCESS",
    "substitutions": {"_MIM_SOURCE_COMMIT": source_commit},
}, indent=2, sort_keys=True) + "\n")
(state_dir / "runtime-build.json").write_text(json.dumps({
    "id": "runtime-build-654321",
    "results": {"images": [{"digest": "sha256:" + "a" * 64, "name": runtime_tag}]},
    "serviceAccount": build_sa,
    "status": "SUCCESS",
    "substitutions": {"_MIM_SOURCE_COMMIT": source_commit},
}, indent=2, sort_keys=True) + "\n")
(state_dir / "builder-artifact.json").write_text(json.dumps({
    "image_summary": {"fully_qualified_digest": builder_uri}
}, indent=2, sort_keys=True) + "\n")
(state_dir / "app-gateway-artifact.json").write_text(json.dumps({
    "image_summary": {"fully_qualified_digest": "asia-northeast3-docker.pkg.dev/" + project_id + "/mim-platform/app-gateway@sha256:" + "b" * 64}
}, indent=2, sort_keys=True) + "\n")
(state_dir / "runtime-artifact.json").write_text(json.dumps({
    "image_summary": {"fully_qualified_digest": runtime_uri}
}, indent=2, sort_keys=True) + "\n")
(state_dir / "bootstrap-secret.json").write_text(json.dumps({
    "name": f"projects/{project_id}/secrets/mim-runtime-bootstrap/versions/7",
    "state": "ENABLED",
}, indent=2, sort_keys=True) + "\n")
(state_dir / "app-gateway-proof-secret.json").write_text(json.dumps({
    "name": f"projects/{project_id}/secrets/mim-app-gateway-origin-v1/versions/11",
    "state": "ENABLED",
}, indent=2, sort_keys=True) + "\n")
PY

  write_project_policy_json
  write_sa_policy_json "$STATE_DIR/release-iam.json" "roles/iam.serviceAccountTokenCreator" "user:$TASK18_OPERATOR_EMAIL"
  write_sa_policy_json "$STATE_DIR/maintenance-iam.json" "roles/iam.serviceAccountUser" "serviceAccount:$RELEASE_EMAIL"

  write_service_json "$STATE_DIR/mim-control-plane.service.json" \
    "mim-control-plane" \
    "mim-control-plane@$TASK18_PROJECT_ID.iam.gserviceaccount.com" \
    "$TASK18_REVIEWED_RUNTIME_IMAGE_URI" \
    "control-plane" \
    "all"
  write_iam_json "$STATE_DIR/mim-control-plane.iam.json" "allUsers"

  write_service_json "$STATE_DIR/mim-app-gateway.service.json" \
    "mim-app-gateway" \
    "mim-app-gateway@$TASK18_PROJECT_ID.iam.gserviceaccount.com" \
    "$TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI" \
    "app-gateway" \
    "all"
  write_iam_json "$STATE_DIR/mim-app-gateway.iam.json" "allUsers"

  write_service_json "$STATE_DIR/mim-deploy-worker.service.json" \
    "mim-deploy-worker" \
    "mim-deploy-worker@$TASK18_PROJECT_ID.iam.gserviceaccount.com" \
    "$TASK18_REVIEWED_RUNTIME_IMAGE_URI" \
    "deploy-worker" \
    "internal"
  write_iam_json "$STATE_DIR/mim-deploy-worker.iam.json" "serviceAccount:mim-deploy-worker@$TASK18_PROJECT_ID.iam.gserviceaccount.com"

  write_service_json "$STATE_DIR/mim-schedule-gateway.service.json" \
    "mim-schedule-gateway" \
    "mim-schedule-gateway@$TASK18_PROJECT_ID.iam.gserviceaccount.com" \
    "$TASK18_REVIEWED_RUNTIME_IMAGE_URI" \
    "schedule-gateway" \
    "internal"
  write_iam_json "$STATE_DIR/mim-schedule-gateway.iam.json" "serviceAccount:$MAINTENANCE_EMAIL" "serviceAccount:mim-app-gateway@$TASK18_PROJECT_ID.iam.gserviceaccount.com"

  write_job_json "$STATE_DIR/$JOB_IDENTITY_SYNC.job.json" "$JOB_IDENTITY_SYNC"
  write_job_json "$STATE_DIR/$JOB_LIFECYCLE.job.json" "$JOB_LIFECYCLE"
  write_job_json "$STATE_DIR/$JOB_USAGE_INGEST.job.json" "$JOB_USAGE_INGEST"

  write_scheduler_json "$STATE_DIR/$JOB_IDENTITY_SYNC.scheduler.json" "$JOB_IDENTITY_SYNC"
  write_scheduler_json "$STATE_DIR/$JOB_LIFECYCLE.scheduler.json" "$JOB_LIFECYCLE"
  write_scheduler_json "$STATE_DIR/$JOB_USAGE_INGEST.scheduler.json" "$JOB_USAGE_INGEST"
}

run_plan_to() {
  local plan_path=$1
  PATH="$STUB_BIN:$PATH" \
    TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
    STATE_DIR="$STATE_DIR" \
    GCLOUD_LOG="$GCLOUD_LOG" \
    CURL_LOG="$CURL_LOG" \
    SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
    MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
    MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI="$TASK18_REVIEWED_BUILDER_IMAGE_URI" \
    MIM_TASK18_REVIEWED_BUILDER_BUILD_ID="$TASK18_REVIEWED_BUILDER_BUILD_ID" \
    MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI="$TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI" \
    MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID="$TASK18_REVIEWED_APP_GATEWAY_BUILD_ID" \
    MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI="$TASK18_REVIEWED_RUNTIME_IMAGE_URI" \
    MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID="$TASK18_REVIEWED_RUNTIME_BUILD_ID" \
    MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION="$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" \
    MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION="$TASK18_APP_GATEWAY_PROOF_SECRET_VERSION" \
    MIM_TASK18_APP_CLOUDFLARE_ACCESS_ISSUER="$TASK18_APP_CLOUDFLARE_ACCESS_ISSUER" \
    MIM_TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE="$TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE" \
    MIM_TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID="$TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID" \
    MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID="$TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID" \
    MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION="$TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION" \
    MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
    MIM_CONFIG_FILE="$TMP_DIR/release.env" \
    MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
    bash "$PLAN_SCRIPT" --plan --out "$plan_path" >/dev/null
}

run_apply() {
  local out_path=$1
  shift
  set +e
  PATH="$STUB_BIN:$PATH" \
    TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
    STATE_DIR="$STATE_DIR" \
    GCLOUD_LOG="$GCLOUD_LOG" \
    CURL_LOG="$CURL_LOG" \
    SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
    MIM_CONFIG_FILE="$TMP_DIR/release.env" \
    MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
    MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI="$TASK18_REVIEWED_BUILDER_IMAGE_URI" \
    MIM_TASK18_REVIEWED_BUILDER_BUILD_ID="$TASK18_REVIEWED_BUILDER_BUILD_ID" \
    MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI="$TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI" \
    MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID="$TASK18_REVIEWED_APP_GATEWAY_BUILD_ID" \
    MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI="$TASK18_REVIEWED_RUNTIME_IMAGE_URI" \
    MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID="$TASK18_REVIEWED_RUNTIME_BUILD_ID" \
    MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION="$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" \
    MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION="$TASK18_APP_GATEWAY_PROOF_SECRET_VERSION" \
    MIM_TASK18_APP_CLOUDFLARE_ACCESS_ISSUER="$TASK18_APP_CLOUDFLARE_ACCESS_ISSUER" \
    MIM_TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE="$TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE" \
    MIM_TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID="$TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID" \
    MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID="$TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID" \
    MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION="$TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION" \
    bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" "$@" >"$out_path" 2>&1
  local exit_code=$?
  set -e
  printf '%s' "$exit_code"
}

task18_write_valid_config "$TMP_DIR/release.env"
task18_write_google_only_config "$TMP_DIR/google-only.env"
task18_write_protected_file "$TMP_DIR/release.protected"
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
write_default_state
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
run_plan_to "$PLAN_PATH"

: >"$GCLOUD_LOG"
rm -f "$NOOP_PLAN_PATH" "$NOOP_PLAN_PATH.sha256"
run_plan_to "$NOOP_PLAN_PATH"
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_PROJECT_ID="$TASK18_PROJECT_ID" \
  PLAN_PROJECT_NUMBER=987654321012 \
  PLAN_APP_ACCESS_ISSUER="$TASK18_APP_CLOUDFLARE_ACCESS_ISSUER" \
  PLAN_APP_ACCESS_AUDIENCE="$TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE" \
  PLAN_APP_GATEWAY_CURRENT_KEY_ID="$TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID" \
  PLAN_APP_GATEWAY_PREVIOUS_KEY_ID= \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$NOOP_PLAN_PATH" >"$TMP_DIR/noop-success.out" 2>&1
noop_exit=$?
set -e
[[ "$noop_exit" -eq 0 ]] || { printf 'FAIL noop_success: expected success\n' >&2; cat "$TMP_DIR/noop-success.out" >&2 || true; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/noop-success.out" "Applied reviewed plan." noop_success || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "run deploy" noop_success || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "add-iam-policy-binding" noop_success || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "run jobs deploy" noop_success || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "scheduler jobs create http" noop_success || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "scheduler jobs update http" noop_success || FAILURES=$((FAILURES + 1))

write_default_state
saved_previous_key_id=$TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID
saved_previous_secret_version=$TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION
TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID=
TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION=
rm -f "$NO_PREVIOUS_PROOF_PLAN_PATH" "$NO_PREVIOUS_PROOF_PLAN_PATH.sha256"
rm -f "$STATE_DIR/mim-app-gateway.service.json" "$STATE_DIR/mim-app-gateway.iam.json"
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI="$TASK18_REVIEWED_BUILDER_IMAGE_URI" \
  MIM_TASK18_REVIEWED_BUILDER_BUILD_ID="$TASK18_REVIEWED_BUILDER_BUILD_ID" \
  MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI="$TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI" \
  MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID="$TASK18_REVIEWED_APP_GATEWAY_BUILD_ID" \
  MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI="$TASK18_REVIEWED_RUNTIME_IMAGE_URI" \
  MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID="$TASK18_REVIEWED_RUNTIME_BUILD_ID" \
  MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION="$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" \
  MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION="$TASK18_APP_GATEWAY_PROOF_SECRET_VERSION" \
  MIM_TASK18_APP_CLOUDFLARE_ACCESS_ISSUER="$TASK18_APP_CLOUDFLARE_ACCESS_ISSUER" \
  MIM_TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE="$TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE" \
  MIM_TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID="$TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID" \
  MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID= \
  MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION= \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$PLAN_SCRIPT" --plan --out "$NO_PREVIOUS_PROOF_PLAN_PATH" >"$TMP_DIR/no-previous-proof-plan.out" 2>&1
: >"$GCLOUD_LOG"
TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID=$saved_previous_key_id
TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION=$saved_previous_secret_version
task18_assert_contains "$APPLY_SCRIPT" 'if os.environ.get("PLAN_APP_GATEWAY_PREVIOUS_KEY_ID"):' no_previous_proof_apply_contract || FAILURES=$((FAILURES + 1))
task18_assert_contains "$APPLY_SCRIPT" 'if [[ -n "${PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION:-}" ]]; then' no_previous_proof_apply_contract || FAILURES=$((FAILURES + 1))
task18_assert_contains "$APPLY_SCRIPT" 'secret_env="MIM_APP_PROOF_CURRENT_SECRET=' no_previous_proof_apply_contract || FAILURES=$((FAILURES + 1))

write_default_state
rm -f "$GOOGLE_ONLY_PLAN_PATH" "$GOOGLE_ONLY_PLAN_PATH.sha256"
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI="$TASK18_REVIEWED_BUILDER_IMAGE_URI" \
  MIM_TASK18_REVIEWED_BUILDER_BUILD_ID="$TASK18_REVIEWED_BUILDER_BUILD_ID" \
  MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI="$TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI" \
  MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID="$TASK18_REVIEWED_APP_GATEWAY_BUILD_ID" \
  MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI="$TASK18_REVIEWED_RUNTIME_IMAGE_URI" \
  MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID="$TASK18_REVIEWED_RUNTIME_BUILD_ID" \
  MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION="$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" \
  MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION="$TASK18_APP_GATEWAY_PROOF_SECRET_VERSION" \
  MIM_TASK18_APP_CLOUDFLARE_ACCESS_ISSUER="$TASK18_APP_CLOUDFLARE_ACCESS_ISSUER" \
  MIM_TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE="$TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE" \
  MIM_TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID="$TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID" \
  MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID="$TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID" \
  MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION="$TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION" \
  MIM_CONFIG_FILE="$TMP_DIR/google-only.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$PLAN_SCRIPT" --plan --out "$GOOGLE_ONLY_PLAN_PATH" >"$TMP_DIR/google-only-plan.out" 2>&1
: >"$GCLOUD_LOG"
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_CONFIG_FILE="$TMP_DIR/google-only.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$GOOGLE_ONLY_PLAN_PATH" >"$TMP_DIR/google-only-apply.out" 2>&1
google_only_apply_exit=$?
set -e
[[ "$google_only_apply_exit" -eq 0 ]] || { printf 'FAIL google_only_apply: expected success\n' >&2; cat "$TMP_DIR/google-only-apply.out" >&2 || true; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/google-only-apply.out" "Applied reviewed plan." google_only_apply || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "slack" google_only_apply || FAILURES=$((FAILURES + 1))

write_default_state
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/no-mutations.out" 2>&1
mutations_exit=$?
set -e
[[ "$mutations_exit" -ne 0 ]] || { printf 'FAIL missing_mutations: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/no-mutations.out" "MIM_ENABLE_MUTATIONS must be exactly true" missing_mutations || FAILURES=$((FAILURES + 1))

set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/no-commit.out" 2>&1
commit_exit=$?
set -e
[[ "$commit_exit" -ne 0 ]] || { printf 'FAIL missing_commit: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/no-commit.out" "Reviewed platform commit must match the reviewed plan commit" missing_commit || FAILURES=$((FAILURES + 1))

write_default_state
python3 - "$STATE_DIR/mim-deploy-worker.iam.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.write_text(json.dumps({"bindings": []}, indent=2, sort_keys=True) + "\n")
PY
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
run_plan_to "$PLAN_PATH"
: >"$GCLOUD_LOG"
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/service-bind-success.out" 2>&1
service_bind_exit=$?
set -e
[[ "$service_bind_exit" -eq 0 ]] || { printf 'FAIL service_bind_success: expected success\n' >&2; cat "$TMP_DIR/service-bind-success.out" >&2 || true; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$GCLOUD_LOG" "add-iam-policy-binding mim-deploy-worker" service_bind_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--impersonate-service-account=mim-release@$TASK18_PROJECT_ID.iam.gserviceaccount.com" service_bind_success || FAILURES=$((FAILURES + 1))

write_default_state
rm -f "$STATE_DIR/mim-deploy-worker.service.json" "$STATE_DIR/mim-deploy-worker.iam.json"
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
run_plan_to "$PLAN_PATH"
: >"$GCLOUD_LOG"
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/service-deploy-success.out" 2>&1
service_deploy_exit=$?
set -e
[[ "$service_deploy_exit" -eq 0 ]] || { printf 'FAIL service_deploy_success: expected success\n' >&2; cat "$TMP_DIR/service-deploy-success.out" >&2 || true; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$GCLOUD_LOG" "run deploy mim-deploy-worker" service_deploy_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "add-iam-policy-binding mim-deploy-worker" service_deploy_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--impersonate-service-account=mim-release@$TASK18_PROJECT_ID.iam.gserviceaccount.com" service_deploy_success || FAILURES=$((FAILURES + 1))

write_default_state
rm -f "$STATE_DIR/$JOB_IDENTITY_SYNC.job.json" "$STATE_DIR/$JOB_IDENTITY_SYNC.scheduler.json"
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
run_plan_to "$PLAN_PATH"
: >"$GCLOUD_LOG"
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/create-success.out" 2>&1
create_exit=$?
set -e
[[ "$create_exit" -eq 0 ]] || { printf 'FAIL create_success: expected success\n' >&2; cat "$TMP_DIR/create-success.out" >&2 || true; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$GCLOUD_LOG" "run jobs deploy mim-identity-sync" create_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--service-account=mim-maintenance@$TASK18_PROJECT_ID.iam.gserviceaccount.com" create_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--args=-m,mim_control_plane.jobs.directory_sync" create_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "scheduler jobs create http mim-identity-sync" create_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--schedule=*/15 * * * *" create_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--uri=https://run.googleapis.com/v2/projects/$TASK18_PROJECT_ID/locations/$MIM_TASK18_FIXED_REGION/jobs/mim-identity-sync:run" create_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--oauth-service-account-email=mim-maintenance@$TASK18_PROJECT_ID.iam.gserviceaccount.com" create_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--impersonate-service-account=mim-release@$TASK18_PROJECT_ID.iam.gserviceaccount.com" create_success || FAILURES=$((FAILURES + 1))

write_default_state
python3 - "$STATE_DIR/$JOB_USAGE_INGEST.scheduler.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())
payload["schedule"] = "0 * * * *"
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
run_plan_to "$PLAN_PATH"
: >"$GCLOUD_LOG"
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/update-success.out" 2>&1
update_exit=$?
set -e
[[ "$update_exit" -eq 0 ]] || { printf 'FAIL update_success: expected success\n' >&2; cat "$TMP_DIR/update-success.out" >&2 || true; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$GCLOUD_LOG" "scheduler jobs update http mim-usage-ingest" update_success || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$GCLOUD_LOG" "scheduler jobs create http mim-usage-ingest" update_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--schedule=12 * * * *" update_success || FAILURES=$((FAILURES + 1))
task18_assert_contains "$GCLOUD_LOG" "--impersonate-service-account=mim-release@$TASK18_PROJECT_ID.iam.gserviceaccount.com" update_success || FAILURES=$((FAILURES + 1))

write_default_state
rm -f "$STATE_DIR/$JOB_LIFECYCLE.job.json"
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
run_plan_to "$PLAN_PATH"
: >"$GCLOUD_LOG"
set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  GCLOUD_DISABLE_READBACK_UPDATE=true \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/readback-fail.out" 2>&1
readback_exit=$?
set -e
[[ "$readback_exit" -ne 0 ]] || { printf 'FAIL readback_verification: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/readback-fail.out" "Readback verification failed" readback_verification || FAILURES=$((FAILURES + 1))

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())
payload["generated_at_epoch"] -= 4000
payload["expires_at_epoch"] = payload["generated_at_epoch"] + 1800
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
printf '%s  %s\n' "$(mim_task18_sha256_file "$PLAN_PATH")" "$(basename "$PLAN_PATH")" >"$PLAN_PATH.sha256"
chmod 600 "$PLAN_PATH.sha256"
set +e
stale_exit=$(PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/stale.out" 2>&1; printf '%s' "$?")
set -e
[[ "$stale_exit" -ne 0 ]] || { printf 'FAIL stale_plan: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/stale.out" "Plan is older than 30 minutes" stale_plan || FAILURES=$((FAILURES + 1))

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())
payload["generated_at_epoch"] += 4000
payload["expires_at_epoch"] = payload["generated_at_epoch"] + 1800
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
printf '%s  %s\n' "$(mim_task18_sha256_file "$PLAN_PATH")" "$(basename "$PLAN_PATH")" >"$PLAN_PATH.sha256"
chmod 600 "$PLAN_PATH.sha256"
printf '\n' >"$PLAN_PATH"

set +e
PATH="$STUB_BIN:$PATH" \
  TASK18_SOURCE_COMMIT="$TASK18_SOURCE_COMMIT" \
  STATE_DIR="$STATE_DIR" \
  GCLOUD_LOG="$GCLOUD_LOG" \
  CURL_LOG="$CURL_LOG" \
  SLACK_CAPTURE_DIR="$SLACK_CAPTURE_DIR" \
  MIM_ENABLE_MUTATIONS=true \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT="$TASK18_SOURCE_COMMIT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="$TASK18_SLACK_CONFIG_TOKEN" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="$TENANT_EVIDENCE_PATH" \
  MIM_CONFIG_FILE="$TMP_DIR/release.env" \
  MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/release.protected" \
  bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/tamper.out" 2>&1
tamper_exit=$?
set -e
[[ "$tamper_exit" -ne 0 ]] || { printf 'FAIL tampered_plan: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/tamper.out" "Plan hash verification failed" tampered_plan || FAILURES=$((FAILURES + 1))

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s release apply assertions failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS test_apply.sh\n'
