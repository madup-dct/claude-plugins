#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
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
NOW_EPOCH=$(date +%s)

PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-$STATE_TOKEN.json"
STALE_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-stale-$STATE_TOKEN.json"
BUILD_MISMATCH_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-build-mismatch-$STATE_TOKEN.json"
BOOTSTRAP_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-bootstrap-disabled-$STATE_TOKEN.json"
GOOGLE_ONLY_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-google-only-$STATE_TOKEN.json"
NO_PREVIOUS_PROOF_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-no-previous-proof-$STATE_TOKEN.json"
IAM_DRIFT_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-iam-drift-$STATE_TOKEN.json"
INVOKER_DRIFT_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-invoker-drift-$STATE_TOKEN.json"
MISSING_SERVICE_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-missing-service-$STATE_TOKEN.json"
MISSING_JOB_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-missing-job-$STATE_TOKEN.json"
SCHEDULER_DRIFT_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-scheduler-drift-$STATE_TOKEN.json"
LEGACY_IAM_PLAN_PATH="$SCRIPT_DIR/.state/test-release-plan-legacy-iam-$STATE_TOKEN.json"
TENANT_EVIDENCE_PATH="$SCRIPT_DIR/.state/test-tenant-evidence-$STATE_TOKEN.json"
FAILURES=0

trap 'rm -rf "$TMP_DIR"; rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" "$STALE_PLAN_PATH" "$STALE_PLAN_PATH.sha256" "$BUILD_MISMATCH_PLAN_PATH" "$BUILD_MISMATCH_PLAN_PATH.sha256" "$BOOTSTRAP_PLAN_PATH" "$BOOTSTRAP_PLAN_PATH.sha256" "$GOOGLE_ONLY_PLAN_PATH" "$GOOGLE_ONLY_PLAN_PATH.sha256" "$NO_PREVIOUS_PROOF_PLAN_PATH" "$NO_PREVIOUS_PROOF_PLAN_PATH.sha256" "$IAM_DRIFT_PLAN_PATH" "$IAM_DRIFT_PLAN_PATH.sha256" "$INVOKER_DRIFT_PLAN_PATH" "$INVOKER_DRIFT_PLAN_PATH.sha256" "$MISSING_SERVICE_PLAN_PATH" "$MISSING_SERVICE_PLAN_PATH.sha256" "$MISSING_JOB_PLAN_PATH" "$MISSING_JOB_PLAN_PATH.sha256" "$SCHEDULER_DRIFT_PLAN_PATH" "$SCHEDULER_DRIFT_PLAN_PATH.sha256" "$LEGACY_IAM_PLAN_PATH" "$LEGACY_IAM_PLAN_PATH.sha256" "$TENANT_EVIDENCE_PATH" "$TENANT_EVIDENCE_PATH.sha256" 2>/dev/null || true' EXIT

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
    cat "${STATE_DIR:?}/mim-app-gateway.service.json"
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
    cat "${STATE_DIR:?}/mim-app-gateway.iam.json"
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

task18_job_module() {
  case "$1" in
    "$JOB_IDENTITY_SYNC") printf 'mim_control_plane.jobs.directory_sync' ;;
    "$JOB_LIFECYCLE") printf 'mim_control_plane.jobs.lifecycle' ;;
    "$JOB_USAGE_INGEST") printf 'mim_control_plane.jobs.usage_ingest' ;;
    *) return 1 ;;
  esac
}

task18_job_runtime_mode() {
  case "$1" in
    "$JOB_IDENTITY_SYNC") printf 'identity-sync' ;;
    "$JOB_LIFECYCLE") printf 'lifecycle' ;;
    "$JOB_USAGE_INGEST") printf 'usage-ingest' ;;
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

task18_job_run_uri() {
  printf 'https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run' \
    "$TASK18_PROJECT_ID" "$MIM_TASK18_FIXED_REGION" "$1"
}

write_job_json() {
  local path=$1
  local job_name=$2
  local runtime_mode module
  runtime_mode=$(task18_job_runtime_mode "$job_name")
  module=$(task18_job_module "$job_name")
  python3 - "$path" "$job_name" "$runtime_mode" "$module" "$TASK18_REVIEWED_RUNTIME_IMAGE_URI" "$TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION" "$MAINTENANCE_EMAIL" "$TASK18_PROJECT_ID" "$MIM_TASK18_FIXED_REGION" <<'PY'
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
  python3 - "$path" "$job_name" "$schedule" "$(task18_job_run_uri "$job_name")" "$MAINTENANCE_EMAIL" "$TASK18_PROJECT_ID" "$MIM_TASK18_FIXED_REGION" <<'PY'
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
run_app_suffix = ".run" + ".app"
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
        {"name": "MIM_APP_AUTHORIZATION_URL", "value": f"https://mim-schedule-gateway-{project_number}.asia-northeast3{run_app_suffix}/v1/apps/authorize"},
        {"name": "MIM_APP_AUTHORIZATION_AUDIENCE", "value": f"https://mim-schedule-gateway-{project_number}.asia-northeast3{run_app_suffix}"},
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
  python3 - "$STATE_DIR/project-iam.json" "$TASK18_PROJECT_ID" "$MIM_TASK18_FIXED_REGION" "$RELEASE_EMAIL" "$MAINTENANCE_EMAIL" "${1:-exact}" <<'PY'
import json
import sys
from pathlib import Path

path, project_id, region, release_email, maintenance_email, mode = sys.argv[1:]
release_member = f"serviceAccount:{release_email}"
maintenance_member = f"serviceAccount:{maintenance_email}"
job_expression = (
    f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-identity-sync" || '
    f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-lifecycle" || '
    f'resource.name == "projects/{project_id}/locations/{region}/jobs/mim-usage-ingest"'
)
bindings = [
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
if mode == "drift":
    bindings.append({"role": "roles/viewer", "members": [release_member]})
elif mode == "legacy":
    bindings[1]["condition"] = {
        "title": "mim-release-schedules",
        "expression": (
            f'resource.name == "projects/{project_id}/locations/{region}" || '
            + job_expression
        ),
    }
    bindings[2]["role"] = "roles/run.jobsExecutorWithOverrides"
    bindings[2]["condition"]["title"] = "mim-maintenance-jobs"
payload = {"bindings": bindings}
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

  write_project_policy_json exact
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

run_plan() {
  local out_path=$1
  local config_path=$2
  local protected_path=$3
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
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    bash "$PLAN_SCRIPT" --plan --out "$out_path"
}

task18_write_valid_config "$TMP_DIR/release.env"
task18_write_protected_file "$TMP_DIR/release.protected"
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
write_default_state

set +e
run_plan "$PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/release.out" 2>&1
release_exit=$?
set -e
if [[ "$release_exit" -ne 0 ]]; then
  printf 'FAIL writes_release_plan: expected exit 0 got %s\n' "$release_exit" >&2
  cat "$TMP_DIR/release.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$PLAN_PATH" "$TENANT_EVIDENCE_PATH" "$RELEASE_EMAIL" "$MAINTENANCE_EMAIL" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
evidence_hash = hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()
release_email = sys.argv[3]
maintenance_email = sys.argv[4]
assert plan["version"] == "mim-release-plan-v6"
assert plan["status"] == "ready"
assert plan["actions"] == []
assert plan["targets"]["app_gateway_build_id"] == "app-gateway-build-222333"
assert plan["targets"]["runtime_build_id"] == "runtime-build-654321"
assert plan["targets"]["runtime_bootstrap_secret_version"] == "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7"
assert plan["targets"]["app_gateway_proof_secret_version"] == "projects/mim-prod-123456/secrets/mim-app-gateway-origin-v1/versions/11"
assert plan["constraints"]["mutation_operator_email"] == "operator.test@madup.com"
assert plan["constraints"]["mutation_impersonation_service_account"] == release_email
assert plan["constraints"]["maintenance_service_account"] == maintenance_email
assert [item["name"] for item in plan["desired_services"]] == ["mim-control-plane", "mim-app-gateway", "mim-deploy-worker", "mim-schedule-gateway"]
assert plan["desired_services"][0]["allow_unauthenticated"] is True
assert plan["desired_services"][1]["allow_unauthenticated"] is True
assert plan["desired_services"][1]["image_uri"] == "asia-northeast3-docker.pkg.dev/mim-prod-123456/mim-platform/app-gateway@sha256:" + "b" * 64
assert plan["desired_services"][2]["allow_unauthenticated"] is False
assert plan["desired_services"][2]["env"] == {
    "MIM_RUNTIME_MODE": "deploy-worker",
    "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7",
    "MIM_ENABLE_MUTATIONS": "true",
}
assert [item["name"] for item in plan["desired_jobs"]] == ["mim-identity-sync", "mim-lifecycle", "mim-usage-ingest"]
assert plan["desired_jobs"][0]["command"] == ["python", "-m", "mim_control_plane.jobs.directory_sync"]
assert plan["desired_jobs"][1]["env"] == {
    "MIM_ENABLE_MUTATIONS": "true",
    "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7",
    "MIM_RUNTIME_MODE": "lifecycle",
}
assert plan["desired_schedulers"][0]["schedule"] == "*/15 * * * *"
assert plan["desired_schedulers"][0]["time_zone"] == "UTC"
assert plan["desired_schedulers"][0]["uri"] == "https://run.googleapis.com/v2/projects/mim-prod-123456/locations/asia-northeast3/jobs/mim-identity-sync:run"
assert plan["initial_state"]["slack_tenant_allowlist_evidence"]["evidence_hash"] == evidence_hash
assert plan["initial_state"]["iam"]["status"] == "ready"
assert not any(item["role"] == "roles/cloudscheduler.admin" and item["title"] is not None for item in plan["initial_state"]["iam"]["expected_release_bindings"])
assert any(item["role"] == "roles/cloudscheduler.admin" and item["title"] is None for item in plan["initial_state"]["iam"]["expected_release_bindings"])
assert plan["initial_state"]["iam"]["maintenance_executor_exact"] is True
assert [item["name"] for item in plan["initial_state"]["runtime_services"]] == ["mim-control-plane", "mim-app-gateway", "mim-deploy-worker", "mim-schedule-gateway"]
assert [item["name"] for item in plan["initial_state"]["runtime_jobs"]] == ["mim-identity-sync", "mim-lifecycle", "mim-usage-ingest"]
assert "xoxe." + "xoxp" + "-task18-config-token" not in json.dumps(plan, sort_keys=True)
PY
fi
task18_assert_contains "$CURL_LOG" "https://slack.com/api/apps.manifest.export" writes_release_plan || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$CURL_LOG" "$TASK18_SLACK_CONFIG_TOKEN" writes_release_plan || FAILURES=$((FAILURES + 1))
if grep -v -- '--account=operator.test@madup.com' "$GCLOUD_LOG" >/dev/null 2>&1; then
  printf 'FAIL writes_release_plan: discovery commands must pin --account\n' >&2
  cat "$GCLOUD_LOG" >&2 || true
  FAILURES=$((FAILURES + 1))
fi
task18_assert_not_contains "$GCLOUD_LOG" "--impersonate-service-account=" writes_release_plan || FAILURES=$((FAILURES + 1))

write_default_state
saved_previous_key_id=$TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID
saved_previous_secret_version=$TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION
TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID=
TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION=
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
: >"$CURL_LOG"
set +e
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
  bash "$PLAN_SCRIPT" --plan --out "$NO_PREVIOUS_PROOF_PLAN_PATH" >"$TMP_DIR/no-previous-proof.out" 2>&1
no_previous_exit=$?
set -e
TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID=$saved_previous_key_id
TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION=$saved_previous_secret_version
if [[ "$no_previous_exit" -ne 0 ]]; then
  printf 'FAIL no_previous_proof_plan: expected exit 0 got %s\n' "$no_previous_exit" >&2
  cat "$TMP_DIR/no-previous-proof.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$NO_PREVIOUS_PROOF_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
targets = plan["targets"]
app_gateway_env = plan["desired_services"][1]["env"]
assert targets["app_gateway_previous_key_id"] == ""
assert targets["app_gateway_previous_secret_version"] == ""
assert "MIM_APP_PROOF_PREVIOUS_KEY_ID" not in app_gateway_env
assert "MIM_APP_PROOF_PREVIOUS_SECRET" not in app_gateway_env
PY
fi

write_default_state
task18_write_google_only_config "$TMP_DIR/google-only.env"
: >"$CURL_LOG"
set +e
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
  bash "$PLAN_SCRIPT" --plan --out "$GOOGLE_ONLY_PLAN_PATH" >"$TMP_DIR/google-only.out" 2>&1
google_only_exit=$?
set -e
if [[ "$google_only_exit" -ne 0 ]]; then
  printf 'FAIL google_only_default: expected exit 0 got %s\n' "$google_only_exit" >&2
  cat "$TMP_DIR/google-only.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$GOOGLE_ONLY_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["initial_state"]["slack"] == {"enabled": False}
assert "slack_app" not in plan["initial_state"]
assert "slack_tenant_allowlist_evidence" not in plan["initial_state"]
PY
fi
if [[ -s "$CURL_LOG" ]]; then
  printf 'FAIL google_only_default: Slack manifest export should not run when disabled\n' >&2
  cat "$CURL_LOG" >&2 || true
  FAILURES=$((FAILURES + 1))
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 4000))"
set +e
run_plan "$STALE_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/stale.out" 2>&1
stale_exit=$?
set -e
if [[ "$stale_exit" -ne 0 ]]; then
  printf 'FAIL stale_tenant_evidence: expected exit 0 got %s\n' "$stale_exit" >&2
  cat "$TMP_DIR/stale.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$STALE_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "stale-slack-tenant-evidence" for item in plan["blockers"])
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
python3 - "$STATE_DIR/runtime-build.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())
payload["results"]["images"][0]["digest"] = "sha256:" + "b" * 64
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
set +e
run_plan "$BUILD_MISMATCH_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/build-mismatch.out" 2>&1
build_mismatch_exit=$?
set -e
if [[ "$build_mismatch_exit" -ne 0 ]]; then
  printf 'FAIL build_digest_mismatch: expected exit 0 got %s\n' "$build_mismatch_exit" >&2
  cat "$TMP_DIR/build-mismatch.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$BUILD_MISMATCH_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "runtime-build-digest-mismatch" for item in plan["blockers"])
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
python3 - "$STATE_DIR/bootstrap-secret.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())
payload["state"] = "DISABLED"
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
set +e
run_plan "$BOOTSTRAP_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/bootstrap.out" 2>&1
bootstrap_exit=$?
set -e
if [[ "$bootstrap_exit" -ne 0 ]]; then
  printf 'FAIL bootstrap_disabled: expected exit 0 got %s\n' "$bootstrap_exit" >&2
  cat "$TMP_DIR/bootstrap.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$BOOTSTRAP_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "runtime-bootstrap-secret-version-disabled" for item in plan["blockers"])
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
write_iam_json "$STATE_DIR/mim-control-plane.iam.json" "allUsers" "serviceAccount:unexpected@mim-prod-123456.iam.gserviceaccount.com"
set +e
run_plan "$INVOKER_DRIFT_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/invoker.out" 2>&1
invoker_exit=$?
set -e
if [[ "$invoker_exit" -ne 0 ]]; then
  printf 'FAIL invoker_drift: expected exit 0 got %s\n' "$invoker_exit" >&2
  cat "$TMP_DIR/invoker.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$INVOKER_DRIFT_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "control-plane-invoker-drift" for item in plan["blockers"])
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
rm -f "$STATE_DIR/mim-deploy-worker.service.json" "$STATE_DIR/mim-deploy-worker.iam.json"
set +e
run_plan "$MISSING_SERVICE_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/missing-service.out" 2>&1
missing_service_exit=$?
set -e
if [[ "$missing_service_exit" -ne 0 ]]; then
  printf 'FAIL missing_service_drift: expected exit 0 got %s\n' "$missing_service_exit" >&2
  cat "$TMP_DIR/missing-service.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$MISSING_SERVICE_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
kinds = [item["kind"] for item in plan["actions"]]
assert "deploy_deploy_worker" in kinds, kinds
assert "bind_invoker_deploy_worker" in kinds, kinds
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
write_project_policy_json drift
set +e
run_plan "$IAM_DRIFT_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/iam.out" 2>&1
iam_exit=$?
set -e
if [[ "$iam_exit" -ne 0 ]]; then
  printf 'FAIL iam_drift: expected exit 0 got %s\n' "$iam_exit" >&2
  cat "$TMP_DIR/iam.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$IAM_DRIFT_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "release-project-role-drift" for item in plan["blockers"])
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
write_project_policy_json legacy
set +e
run_plan "$LEGACY_IAM_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/legacy-iam.out" 2>&1
legacy_iam_exit=$?
set -e
if [[ "$legacy_iam_exit" -ne 0 ]]; then
  printf 'FAIL legacy_iam_drift: expected exit 0 got %s\n' "$legacy_iam_exit" >&2
  cat "$TMP_DIR/legacy-iam.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$LEGACY_IAM_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "release-project-role-drift" for item in plan["blockers"])
assert any(item["code"] == "maintenance-job-executor-drift" for item in plan["blockers"])
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
rm -f "$STATE_DIR/$JOB_IDENTITY_SYNC.job.json"
set +e
run_plan "$MISSING_JOB_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/missing-job.out" 2>&1
missing_job_exit=$?
set -e
if [[ "$missing_job_exit" -ne 0 ]]; then
  printf 'FAIL missing_job_action: expected exit 0 got %s\n' "$missing_job_exit" >&2
  cat "$TMP_DIR/missing-job.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$MISSING_JOB_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["actions"] == [
    {"kind": "upsert_job", "module": "mim_control_plane.jobs.directory_sync", "name": "mim-identity-sync"}
], plan["actions"]
PY
fi

write_default_state
task18_write_tenant_evidence "$TENANT_EVIDENCE_PATH" "$((NOW_EPOCH - 60))"
python3 - "$STATE_DIR/$JOB_USAGE_INGEST.scheduler.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())
payload["schedule"] = "0 * * * *"
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
set +e
run_plan "$SCHEDULER_DRIFT_PLAN_PATH" "$TMP_DIR/release.env" "$TMP_DIR/release.protected" >"$TMP_DIR/scheduler-drift.out" 2>&1
scheduler_drift_exit=$?
set -e
if [[ "$scheduler_drift_exit" -ne 0 ]]; then
  printf 'FAIL scheduler_drift_action: expected exit 0 got %s\n' "$scheduler_drift_exit" >&2
  cat "$TMP_DIR/scheduler-drift.out" >&2 || true
  FAILURES=$((FAILURES + 1))
else
  python3 - "$SCHEDULER_DRIFT_PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["actions"] == [
    {"kind": "upsert_scheduler", "mode": "update", "name": "mim-usage-ingest"}
], plan["actions"]
PY
fi

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s release plan assertions failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS test_plan.sh\n'
