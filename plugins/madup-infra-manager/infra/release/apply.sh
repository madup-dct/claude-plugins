#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/task18_lib.sh"
CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"

MODE=
PLAN_FILE=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --apply) MODE=apply; shift ;;
    --plan-file) PLAN_FILE=$2; shift 2 ;;
    --*) mim_task18_fail "Unknown argument: $1" ;;
    *) mim_task18_fail "Positional arguments are not supported" ;;
  esac
done
[[ "$MODE" == apply && -n "$PLAN_FILE" ]] || mim_task18_fail "Usage: apply.sh --apply --plan-file .state/<name>.json"
mim_task18_assert_plan_read_path "$SCRIPT_DIR" "$PLAN_FILE"
mim_task18_validate_plan_hash_and_age "$PLAN_FILE"

PLAN_GENERATED_AT=$(python3 - "$PLAN_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["generated_at_epoch"])
PY
)
PLAN_EXPIRES_AT=$(python3 - "$PLAN_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["expires_at_epoch"])
PY
)
status=$(python3 - "$PLAN_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["status"])
PY
)
[[ "$status" == "ready" ]] || mim_task18_fail "Reviewed plan contains blockers"

TMP_DIR=$(mktemp -d)
SNAPSHOT_DIR=$(mktemp -d)
EXPECTED_PATH="$SCRIPT_DIR/.state/task18-release-expected-$$.json"
READBACK_PATH="$SCRIPT_DIR/.state/task18-release-readback-$$.json"
trap 'rm -rf "$TMP_DIR" "$SNAPSHOT_DIR"; rm -f "$EXPECTED_PATH" "$EXPECTED_PATH.sha256" "$READBACK_PATH" "$READBACK_PATH.sha256"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
MIM_CONFIG_FILE="$SNAPSHOT_CONFIG"
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v gcloud >/dev/null 2>&1 || mim_task18_fail "gcloud CLI is required"

python3 - "$PLAN_FILE" <<'PY' >"$TMP_DIR/plan-env.sh"
import json, shlex, sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
targets = plan["targets"]
pairs = {
    "PLAN_SOURCE_COMMIT": targets["source_commit"],
    "PLAN_BUILDER_IMAGE_URI": targets["builder_image_uri"],
    "PLAN_BUILDER_BUILD_ID": targets["builder_build_id"],
    "PLAN_APP_GATEWAY_IMAGE_URI": targets["app_gateway_image_uri"],
    "PLAN_APP_GATEWAY_BUILD_ID": targets["app_gateway_build_id"],
    "PLAN_PROJECT_NUMBER": targets["project_number"],
    "PLAN_RUNTIME_IMAGE_URI": targets["runtime_image_uri"],
    "PLAN_RUNTIME_BUILD_ID": targets["runtime_build_id"],
    "PLAN_RUNTIME_BOOTSTRAP_SECRET_VERSION": targets["runtime_bootstrap_secret_version"],
    "PLAN_APP_GATEWAY_PROOF_SECRET_VERSION": targets["app_gateway_proof_secret_version"],
    "PLAN_APP_ACCESS_ISSUER": targets["app_access_issuer"],
    "PLAN_APP_ACCESS_AUDIENCE": targets["app_access_audience"],
    "PLAN_APP_GATEWAY_CURRENT_KEY_ID": targets["app_gateway_current_key_id"],
    "PLAN_APP_GATEWAY_PREVIOUS_KEY_ID": targets.get("app_gateway_previous_key_id", ""),
    "PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION": targets.get("app_gateway_previous_secret_version", ""),
}
for key, value in pairs.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
. "$TMP_DIR/plan-env.sh"

run_reviewed_plan() {
  local out_path=$1
  MIM_CONFIG_FILE="$SNAPSHOT_CONFIG" \
  MIM_TASK18_PLAN_GENERATED_AT="$PLAN_GENERATED_AT" \
  MIM_TASK18_PLAN_EXPIRES_AT="$PLAN_EXPIRES_AT" \
  MIM_TASK18_SLACK_CONFIG_TOKEN="${MIM_TASK18_SLACK_CONFIG_TOKEN-}" \
  MIM_TASK18_REVIEWED_PLATFORM_COMMIT= \
  MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI="$PLAN_BUILDER_IMAGE_URI" \
  MIM_TASK18_REVIEWED_BUILDER_BUILD_ID="$PLAN_BUILDER_BUILD_ID" \
  MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI="$PLAN_APP_GATEWAY_IMAGE_URI" \
  MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID="$PLAN_APP_GATEWAY_BUILD_ID" \
  MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI="$PLAN_RUNTIME_IMAGE_URI" \
  MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID="$PLAN_RUNTIME_BUILD_ID" \
  MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION="$PLAN_RUNTIME_BOOTSTRAP_SECRET_VERSION" \
  MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION="$PLAN_APP_GATEWAY_PROOF_SECRET_VERSION" \
  MIM_TASK18_APP_CLOUDFLARE_ACCESS_ISSUER="$PLAN_APP_ACCESS_ISSUER" \
  MIM_TASK18_APP_CLOUDFLARE_ACCESS_AUDIENCE="$PLAN_APP_ACCESS_AUDIENCE" \
  MIM_TASK18_APP_GATEWAY_PROOF_CURRENT_KEY_ID="$PLAN_APP_GATEWAY_CURRENT_KEY_ID" \
  MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_KEY_ID="$PLAN_APP_GATEWAY_PREVIOUS_KEY_ID" \
  MIM_TASK18_APP_GATEWAY_PROOF_PREVIOUS_SECRET_VERSION="$PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION" \
  MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE="${MIM_TASK18_SLACK_TENANT_EVIDENCE_FILE-}" \
  bash "$SCRIPT_DIR/plan.sh" --plan --out "$out_path" >/dev/null
}

run_reviewed_plan "$EXPECTED_PATH"
comparison=$(mim_task18_compare_plans "$PLAN_FILE" "$EXPECTED_PATH")
case "$comparison" in
  ok) ;;
  drift) mim_task18_fail "Discovery drift detected" ;;
  *) mim_task18_fail "Plan file does not match the expected reviewed contract" ;;
esac

mim_task18_require_exact_true_env "MIM_ENABLE_MUTATIONS"
[[ "${MIM_TASK18_REVIEWED_PLATFORM_COMMIT-}" == "$PLAN_SOURCE_COMMIT" ]] || mim_task18_fail "Reviewed platform commit must match the reviewed plan commit"

if [[ -n "${MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI-}" && "${MIM_TASK18_REVIEWED_BUILDER_IMAGE_URI}" != "$PLAN_BUILDER_IMAGE_URI" ]]; then
  mim_task18_fail "Reviewed builder image URI does not match the reviewed plan"
fi
if [[ -n "${MIM_TASK18_REVIEWED_BUILDER_BUILD_ID-}" && "${MIM_TASK18_REVIEWED_BUILDER_BUILD_ID}" != "$PLAN_BUILDER_BUILD_ID" ]]; then
  mim_task18_fail "Reviewed builder Cloud Build ID does not match the reviewed plan"
fi
if [[ -n "${MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI-}" && "${MIM_TASK18_REVIEWED_APP_GATEWAY_IMAGE_URI}" != "$PLAN_APP_GATEWAY_IMAGE_URI" ]]; then
  mim_task18_fail "Reviewed app-gateway image URI does not match the reviewed plan"
fi
if [[ -n "${MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID-}" && "${MIM_TASK18_REVIEWED_APP_GATEWAY_BUILD_ID}" != "$PLAN_APP_GATEWAY_BUILD_ID" ]]; then
  mim_task18_fail "Reviewed app-gateway Cloud Build ID does not match the reviewed plan"
fi
if [[ -n "${MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI-}" && "${MIM_TASK18_REVIEWED_RUNTIME_IMAGE_URI}" != "$PLAN_RUNTIME_IMAGE_URI" ]]; then
  mim_task18_fail "Reviewed runtime image URI does not match the reviewed plan"
fi
if [[ -n "${MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID-}" && "${MIM_TASK18_REVIEWED_RUNTIME_BUILD_ID}" != "$PLAN_RUNTIME_BUILD_ID" ]]; then
  mim_task18_fail "Reviewed runtime Cloud Build ID does not match the reviewed plan"
fi
if [[ -n "${MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION-}" && "${MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_VERSION}" != "$PLAN_RUNTIME_BOOTSTRAP_SECRET_VERSION" ]]; then
  mim_task18_fail "Reviewed runtime bootstrap secret version does not match the reviewed plan"
fi
if [[ -n "${MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION-}" && "${MIM_TASK18_APP_GATEWAY_PROOF_SECRET_VERSION}" != "$PLAN_APP_GATEWAY_PROOF_SECRET_VERSION" ]]; then
  mim_task18_fail "Reviewed app-gateway proof secret version does not match the reviewed plan"
fi

python3 - "$PLAN_FILE" <<'PY' >"$TMP_DIR/actions.tsv"
import json, sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
service_by_name = {item["name"]: item for item in plan.get("desired_services", [])}
job_by_name = {item["name"]: item for item in plan["desired_jobs"]}
scheduler_by_name = {item["name"]: item for item in plan["desired_schedulers"]}
for action in plan["actions"]:
    kind = action["kind"]
    name = action["name"]
    if kind.startswith("deploy_"):
        desired = service_by_name[name]
        print("\t".join([
            kind,
            name,
            desired["runtime_mode"],
            desired["service_account"],
            desired["image_uri"],
            "true" if desired["allow_unauthenticated"] else "false",
            desired["ingress"],
        ]))
    elif kind.startswith("bind_invoker_"):
        print("\t".join([
            kind,
            name,
            action["member"],
        ]))
    elif kind == "upsert_job":
        desired = job_by_name[name]
        print("\t".join([
            kind,
            name,
            desired["command"][2],
            desired["env"]["MIM_RUNTIME_MODE"],
            desired["service_account"],
            desired["image_uri"],
            desired["env"]["MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION"],
        ]))
    elif kind == "upsert_scheduler":
        desired = scheduler_by_name[name]
        print("\t".join([
            kind,
            name,
            action["mode"],
            desired["schedule"],
            desired["uri"],
            desired["oauth_service_account"],
        ]))
    else:
        print("\t".join([kind, name]))
PY

RELEASE_IDENTITY_EMAIL=$(mim_task18_release_identity_email)
IMPERSONATE_FLAG="--impersonate-service-account=$RELEASE_IDENTITY_EMAIL"

deploy_runtime() {
  local service_name=$1
  local runtime_mode=$2
  local service_account=$3
  local image_uri=$4
  local allow_unauth=$5
  local ingress=$6
  local auth_flag=--no-allow-unauthenticated
  if [[ "$allow_unauth" == "true" ]]; then
    auth_flag=--allow-unauthenticated
  fi

  gcloud run deploy "$service_name" \
    --image="$image_uri" \
    --region="$MIM_TASK18_FIXED_REGION" \
    "$auth_flag" \
    --cpu-throttling \
    --no-cpu-boost \
    --min=0 \
    --min-instances=0 \
    --max=1 \
    --max-instances=1 \
    --cpu=1 \
    --memory=512Mi \
    --concurrency=20 \
    --timeout=300 \
    --ingress="$ingress" \
    --service-account="$service_account" \
    --set-env-vars="MIM_RUNTIME_MODE=$runtime_mode,MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION=$PLAN_RUNTIME_BOOTSTRAP_SECRET_VERSION,MIM_ENABLE_MUTATIONS=true" \
    --quiet \
    "$IMPERSONATE_FLAG" \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >/dev/null
}

deploy_app_gateway() {
  local service_name=$1
  local image_uri=$2
  local service_account=$3
  local ingress=$4
  local plain_env
  local secret_env
  plain_env=$(
    python3 - <<'PY'
import os
pairs = {
    "MIM_PUBLIC_SUFFIX": "madup.app",
    "MIM_PROJECT_ID": os.environ["MIM_PROJECT_ID"],
    "MIM_PROJECT_NUMBER": os.environ["PLAN_PROJECT_NUMBER"],
    "MIM_REGION": os.environ["MIM_TASK18_FIXED_REGION"],
    "MIM_CLOUDFLARE_ACCESS_ISSUER": os.environ["PLAN_APP_ACCESS_ISSUER"],
    "MIM_CLOUDFLARE_ACCESS_AUDIENCE": os.environ["PLAN_APP_ACCESS_AUDIENCE"],
    "MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL": f"mim-app-gateway@{os.environ['MIM_PROJECT_ID']}.iam.gserviceaccount.com",
    "MIM_APP_AUTHORIZATION_URL": f"https://mim-schedule-gateway-{os.environ['PLAN_PROJECT_NUMBER']}.{os.environ['MIM_TASK18_FIXED_REGION']}.run.app/v1/apps/authorize",
    "MIM_APP_AUTHORIZATION_AUDIENCE": f"https://mim-schedule-gateway-{os.environ['PLAN_PROJECT_NUMBER']}.{os.environ['MIM_TASK18_FIXED_REGION']}.run.app",
    "MIM_APP_PROOF_CURRENT_KEY_ID": os.environ["PLAN_APP_GATEWAY_CURRENT_KEY_ID"],
}
if os.environ.get("PLAN_APP_GATEWAY_PREVIOUS_KEY_ID"):
    pairs["MIM_APP_PROOF_PREVIOUS_KEY_ID"] = os.environ["PLAN_APP_GATEWAY_PREVIOUS_KEY_ID"]
print(",".join(f"{k}={v}" for k, v in pairs.items()))
PY
  )
  secret_env="MIM_APP_PROOF_CURRENT_SECRET=$(mim_task18_secret_name_from_ref "$PLAN_APP_GATEWAY_PROOF_SECRET_VERSION"):$(mim_task18_secret_version_number "$PLAN_APP_GATEWAY_PROOF_SECRET_VERSION")"
  if [[ -n "${PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION:-}" ]]; then
    secret_env="${secret_env},MIM_APP_PROOF_PREVIOUS_SECRET=$(mim_task18_secret_name_from_ref "$PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION"):$(mim_task18_secret_version_number "$PLAN_APP_GATEWAY_PREVIOUS_SECRET_VERSION")"
  fi

  gcloud run deploy "$service_name" \
    --image="$image_uri" \
    --region="$MIM_TASK18_FIXED_REGION" \
    --allow-unauthenticated \
    --cpu-throttling \
    --no-cpu-boost \
    --min=0 \
    --min-instances=0 \
    --max=1 \
    --max-instances=1 \
    --cpu=1 \
    --memory=512Mi \
    --concurrency=20 \
    --timeout=300 \
    --ingress="$ingress" \
    --service-account="$service_account" \
    --set-env-vars="$plain_env" \
    --update-secrets="$secret_env" \
    --quiet \
    "$IMPERSONATE_FLAG" \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >/dev/null
}

add_invoker_binding() {
  local service_name=$1
  local member=$2
  gcloud run services add-iam-policy-binding "$service_name" \
    --region="$MIM_TASK18_FIXED_REGION" \
    --member="$member" \
    --role=roles/run.invoker \
    --quiet \
    "$IMPERSONATE_FLAG" \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >/dev/null
}

deploy_job() {
  local job_name=$1
  local module=$2
  local runtime_mode=$3
  local service_account=$4
  local image_uri=$5
  local bootstrap_secret_version=$6

  gcloud run jobs deploy "$job_name" \
    --image="$image_uri" \
    --region="$MIM_TASK18_FIXED_REGION" \
    --service-account="$service_account" \
    --tasks=1 \
    --parallelism=1 \
    --max-retries=0 \
    --task-timeout=600s \
    --cpu=1 \
    --memory=512Mi \
    --command=python \
    --args=-m,"$module" \
    --set-env-vars="MIM_RUNTIME_MODE=$runtime_mode,MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION=$bootstrap_secret_version,MIM_ENABLE_MUTATIONS=true" \
    --quiet \
    "$IMPERSONATE_FLAG" \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >/dev/null
}

upsert_scheduler() {
  local mode=$1
  local job_name=$2
  local schedule=$3
  local uri=$4
  local oauth_service_account=$5
  local action=create
  [[ "$mode" == "update" ]] && action=update

  gcloud scheduler jobs "$action" http "$job_name" \
    --location="$MIM_TASK18_FIXED_REGION" \
    --schedule="$schedule" \
    --time-zone=UTC \
    --uri="$uri" \
    --http-method=POST \
    --oauth-service-account-email="$oauth_service_account" \
    --quiet \
    "$IMPERSONATE_FLAG" \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >/dev/null
}

while IFS=$'\t' read -r kind name arg1 arg2 arg3 arg4 arg5; do
  [[ -n "$kind" ]] || continue
  case "$kind" in
    deploy_control_plane|deploy_deploy_worker|deploy_schedule_gateway)
      deploy_runtime "$name" "$arg1" "$arg2" "$arg3" "$arg4" "$arg5"
      ;;
    deploy_app_gateway)
      deploy_app_gateway "$name" "$arg3" "$arg2" "$arg5"
      ;;
    bind_invoker_control_plane|bind_invoker_deploy_worker|bind_invoker_schedule_gateway)
      add_invoker_binding "$name" "$arg1"
      ;;
    upsert_job)
      deploy_job "$name" "$arg1" "$arg2" "$arg3" "$arg4" "$arg5"
      ;;
    upsert_scheduler)
      upsert_scheduler "$arg1" "$name" "$arg2" "$arg3" "$arg4"
      ;;
    *)
      mim_task18_fail "Plan file does not match the expected reviewed contract"
      ;;
  esac
done <"$TMP_DIR/actions.tsv"

run_reviewed_plan "$READBACK_PATH"
python3 - "$READBACK_PATH" <<'PY' >"$TMP_DIR/readback.txt"
import json, sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
print(payload["status"])
print(len(payload["actions"]))
PY
READBACK_STATUS=$(sed -n '1p' "$TMP_DIR/readback.txt")
READBACK_ACTION_COUNT=$(sed -n '2p' "$TMP_DIR/readback.txt")
if [[ "$READBACK_STATUS" != "ready" || "$READBACK_ACTION_COUNT" != "0" ]]; then
  mim_task18_fail "Readback verification failed"
fi

printf 'Applied reviewed plan.\n'
