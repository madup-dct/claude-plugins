#!/usr/bin/env bash

mim_iam_contract_helper() {
  local script_dir=$1
  printf '%s/iam/contract.py' "$script_dir"
}

mim_iam_bq() {
  CLOUDSDK_CORE_ACCOUNT="$MIM_OPERATOR_EMAIL" \
  CLOUDSDK_CORE_PROJECT="$MIM_PROJECT_ID" \
  bq "$@"
}

mim_iam_optional_bq_dataset() {
  local output_file=$1
  local dataset_ref="${MIM_PROJECT_ID}:mim_billing_export"
  local canonical_ref="dataset ${dataset_ref}"
  local stderr_file
  local status=0

  stderr_file=$(mktemp)
  set +e
  mim_iam_bq show --format=prettyjson "$dataset_ref" >"$output_file" 2>"$stderr_file"
  status=$?
  set -e

  if [[ "$status" -eq 0 ]]; then
    rm -f -- "$stderr_file"
    return
  fi

  if grep -Fq -- 'NOT_FOUND:' "$stderr_file" && \
     grep -Fq -- "$canonical_ref" "$stderr_file"; then
    rm -f -- "$stderr_file"
    printf '{}\n' >"$output_file"
    return
  fi

  rm -f -- "$stderr_file"
  mim_fail "Unable to inspect raw billing export dataset"
}

mim_iam_optional_json() {
  local description=$1
  local output_file=$2
  local expected_resource=$3
  shift 3

  local stderr_file
  local status
  stderr_file=$(mktemp)
  set +e
  "$@" >"$output_file" 2>"$stderr_file"
  status=$?
  set -e

  if [[ "$status" -eq 0 ]]; then
    rm -f -- "$stderr_file"
    return
  fi

  if grep -Fq -- 'NOT_FOUND:' "$stderr_file" && \
     grep -Fiq -- "$expected_resource" "$stderr_file"; then
    rm -f -- "$stderr_file"
    printf '{}\n' >"$output_file"
    return
  fi

  rm -f -- "$stderr_file"
  mim_fail "$description"
}

mim_capture_iam_contract() {
  local script_dir=$1
  local tmp_dir=$2
  local project_number=$3
  local helper

  helper=$(mim_iam_contract_helper "$script_dir")
  [[ -f "$helper" ]] || mim_fail "IAM contract helper is required"

  MIM_IAM_PROJECT_POLICY_FILE="$tmp_dir/project-iam.json"
  MIM_IAM_BUILD_POLICY_FILE="$tmp_dir/build-iam.json"
  MIM_IAM_CONTROL_PLANE_POLICY_FILE="$tmp_dir/control-plane-iam.json"
  MIM_IAM_APP_GATEWAY_POLICY_FILE="$tmp_dir/app-gateway-iam.json"
  MIM_IAM_DEPLOY_WORKER_POLICY_FILE="$tmp_dir/deploy-worker-iam.json"
  MIM_IAM_IDENTITY_SYNC_POLICY_FILE="$tmp_dir/identity-sync-iam.json"
  MIM_IAM_MAINTENANCE_POLICY_FILE="$tmp_dir/maintenance-iam.json"
  MIM_IAM_RELEASE_POLICY_FILE="$tmp_dir/release-iam.json"
  MIM_IAM_SCHEDULE_GATEWAY_POLICY_FILE="$tmp_dir/schedule-gateway-iam.json"
  MIM_IAM_ARTIFACT_REPOSITORY_POLICY_FILE="$tmp_dir/artifact-repository-iam.json"
  MIM_IAM_SECRET_POLICIES_TSV_FILE="$tmp_dir/secret-policies.tsv"
  MIM_IAM_BILLING_DATASET_FILE="$tmp_dir/billing-export.json"
  MIM_IAM_EVALUATION_FILE="$tmp_dir/iam-evaluation.json"

  gcloud projects get-iam-policy "$MIM_PROJECT_ID" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >"$MIM_IAM_PROJECT_POLICY_FILE"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-build" \
    "$MIM_IAM_BUILD_POLICY_FILE" \
    "$(mim_identity_email mim-build)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-build)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-control-plane" \
    "$MIM_IAM_CONTROL_PLANE_POLICY_FILE" \
    "$(mim_identity_email mim-control-plane)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-control-plane)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-app-gateway" \
    "$MIM_IAM_APP_GATEWAY_POLICY_FILE" \
    "$(mim_identity_email mim-app-gateway)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-app-gateway)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-deploy-worker" \
    "$MIM_IAM_DEPLOY_WORKER_POLICY_FILE" \
    "$(mim_identity_email mim-deploy-worker)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-deploy-worker)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-identity-sync" \
    "$MIM_IAM_IDENTITY_SYNC_POLICY_FILE" \
    "$(mim_identity_email mim-identity-sync)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-identity-sync)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-maintenance" \
    "$MIM_IAM_MAINTENANCE_POLICY_FILE" \
    "$(mim_identity_email mim-maintenance)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-maintenance)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-release" \
    "$MIM_IAM_RELEASE_POLICY_FILE" \
    "$(mim_identity_email mim-release)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-release)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect IAM policy for managed identity mim-schedule-gateway" \
    "$MIM_IAM_SCHEDULE_GATEWAY_POLICY_FILE" \
    "$(mim_identity_email mim-schedule-gateway)" \
    gcloud iam service-accounts get-iam-policy "$(mim_identity_email mim-schedule-gateway)" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  mim_iam_optional_json \
    "Unable to inspect Artifact Registry IAM policy for repository mim" \
    "$MIM_IAM_ARTIFACT_REPOSITORY_POLICY_FILE" \
    "repository mim" \
    gcloud artifacts repositories get-iam-policy mim \
    --location="$MIM_FIXED_REGION" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID"

  : >"$MIM_IAM_SECRET_POLICIES_TSV_FILE"
  while IFS=$'\t' read -r secret_name secret_mode; do
    local policy_file
    [[ -n "$secret_name" ]] || continue
    policy_file="$tmp_dir/secret-${secret_name}-iam.json"
    mim_iam_optional_json \
      "Unable to inspect Secret Manager IAM policy for secret $secret_name" \
      "$policy_file" \
      "secret $secret_name" \
      gcloud secrets get-iam-policy "$secret_name" \
      '--format=json' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID"
    printf '%s\t%s\t%s\n' "$secret_name" "$secret_mode" "$policy_file" >>"$MIM_IAM_SECRET_POLICIES_TSV_FILE"
  done < <(mim_fixed_secret_iam_rows)

  mim_iam_optional_bq_dataset "$MIM_IAM_BILLING_DATASET_FILE"

  MIM_PROJECT_ID="$MIM_PROJECT_ID" \
  MIM_PROJECT_NUMBER="$project_number" \
  MIM_OPERATOR_EMAIL="$MIM_OPERATOR_EMAIL" \
  MIM_IAM_PROJECT_POLICY_FILE="$MIM_IAM_PROJECT_POLICY_FILE" \
  MIM_IAM_BUILD_POLICY_FILE="$MIM_IAM_BUILD_POLICY_FILE" \
  MIM_IAM_CONTROL_PLANE_POLICY_FILE="$MIM_IAM_CONTROL_PLANE_POLICY_FILE" \
  MIM_IAM_APP_GATEWAY_POLICY_FILE="$MIM_IAM_APP_GATEWAY_POLICY_FILE" \
  MIM_IAM_DEPLOY_WORKER_POLICY_FILE="$MIM_IAM_DEPLOY_WORKER_POLICY_FILE" \
  MIM_IAM_IDENTITY_SYNC_POLICY_FILE="$MIM_IAM_IDENTITY_SYNC_POLICY_FILE" \
  MIM_IAM_MAINTENANCE_POLICY_FILE="$MIM_IAM_MAINTENANCE_POLICY_FILE" \
  MIM_IAM_RELEASE_POLICY_FILE="$MIM_IAM_RELEASE_POLICY_FILE" \
  MIM_IAM_SCHEDULE_GATEWAY_POLICY_FILE="$MIM_IAM_SCHEDULE_GATEWAY_POLICY_FILE" \
  MIM_IAM_ARTIFACT_REPOSITORY_POLICY_FILE="$MIM_IAM_ARTIFACT_REPOSITORY_POLICY_FILE" \
  MIM_IAM_SECRET_POLICIES_TSV_FILE="$MIM_IAM_SECRET_POLICIES_TSV_FILE" \
  MIM_IAM_BILLING_DATASET_FILE="$MIM_IAM_BILLING_DATASET_FILE" \
  python3 "$helper" evaluate >"$MIM_IAM_EVALUATION_FILE"
}

mim_iam_first_issue() {
  python3 - "$MIM_IAM_EVALUATION_FILE" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
blockers = data.get("blockers") or []
if blockers:
    first = blockers[0]
    if isinstance(first, dict) and isinstance(first.get("message"), str):
        print(first["message"])
        raise SystemExit(0)

actions = data.get("actions") or []
if not actions:
    raise SystemExit(1)
first = actions[0]
kind = first.get("kind")
member = str(first.get("member", ""))
name = str(first.get("name", ""))
value = str(first.get("value", ""))
member_name = member.removeprefix("serviceAccount:").split("@", 1)[0]
resource_name = value.split("@", 1)[0]
if kind == "bind_project_role":
    print(f"Managed identity {member_name} must hold project role {name}")
elif kind == "bind_service_account_role":
    print(f"Managed identity {resource_name} must grant {name} to {member}")
elif kind == "bind_artifact_repository_role":
    print(f"Artifact Registry repository {value} must grant {name} to {member}")
elif kind == "bind_secret_resource_role":
    print(f"Secret resource {value} must grant {name} to {member}")
else:
    print("IAM contract findings require review")
PY
}

mim_iam_append_plan_findings() {
  local blocker_file=$1
  local action_file=$2

  python3 - "$MIM_IAM_EVALUATION_FILE" "$blocker_file" "$action_file" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
blocker_path = Path(sys.argv[2])
action_path = Path(sys.argv[3])

with blocker_path.open("a", encoding="utf-8") as blocker_stream:
    for item in data.get("blockers") or []:
        blocker_stream.write(
            "\t".join(
                [
                    str(item.get("code", "iam-contract")),
                    str(item.get("code", "iam-contract")),
                    str(item.get("message", "IAM contract drift detected")),
                ]
            )
            + "\n"
        )

with action_path.open("a", encoding="utf-8") as action_stream:
    for item in data.get("actions") or []:
        action_stream.write(
            "\t".join(
                [
                    str(item.get("kind", "")),
                    str(item.get("name", "")),
                    str(item.get("value", "__empty__")),
                    str(item.get("member", "__empty__")),
                    str(item.get("condition_title", "__empty__")),
                    str(item.get("condition_expression", "__empty__")),
                ]
            )
            + "\n"
        )
PY
}
