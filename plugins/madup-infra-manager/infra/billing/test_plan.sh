#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/test_common.sh"

PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
TMP_DIR=$(mktemp -d)
STATE_DIR="$SCRIPT_DIR/.state"
PLAN_PATH="$STATE_DIR/test-billing-plan-$$.json"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
BQ_LOG="$TMP_DIR/bq.log"
mkdir -p "$STATE_DIR"
trap 'rm -rf "$TMP_DIR"; rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" 2>/dev/null || true' EXIT

CONFIG_FILE="$TMP_DIR/config.env"
billing_write_config "$CONFIG_FILE"

STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"
case "$*" in
  auth\ list\ *"--format=value(account)"*)
    printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-operator.test@madup.com}"
    ;;
  projects\ get-iam-policy\ mim-prod-123456\ *"--format=json"*)
    cat "${PROJECT_IAM_FIXTURE_JSON:?}"
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
printf 'account=%s project=%s %s\n' "${CLOUDSDK_CORE_ACCOUNT:?}" "${CLOUDSDK_CORE_PROJECT:?}" "$*" >> "${BQ_LOG:?}"
case "$1 $2" in
  "show --format=prettyjson")
    case "$3" in
      mim-prod-123456:mim_billing_export)
        cat "${RAW_DATASET_FIXTURE_JSON:?}"
        ;;
      mim-prod-123456:mim_billing_secure)
        if [[ "${SECURE_DATASET_ERROR_MODE:-ok}" == "host_not_found" ]]; then
          printf 'lookup bigquery.googleapis.com: no such host\n' >&2
          exit 1
        elif [[ "${SECURE_DATASET_EXISTS:-false}" == "true" ]]; then
          cat "${SECURE_DATASET_FIXTURE_JSON:?}"
        else
          printf 'NOT_FOUND: Dataset mim-prod-123456:mim_billing_secure was not found\n' >&2
          exit 1
        fi
        ;;
      mim-prod-123456:mim_billing_secure.mim_usage_costs_v1)
        if [[ "${SECURE_VIEW_ERROR_MODE:-ok}" == "host_not_found" ]]; then
          printf 'lookup bigquery.googleapis.com: no such host\n' >&2
          exit 1
        elif [[ "${SECURE_VIEW_EXISTS:-false}" == "true" ]]; then
          cat "${SECURE_VIEW_FIXTURE_JSON:?}"
        else
          printf 'NOT_FOUND: Table mim-prod-123456:mim_billing_secure.mim_usage_costs_v1 was not found\n' >&2
          exit 1
        fi
        ;;
      *)
        printf 'unexpected bq show target: %s\n' "$3" >&2
        exit 99
        ;;
    esac
    ;;
  "show --dataset_view=FULL")
    if [[ "$4" == "mim-prod-123456:mim_billing_export" ]]; then
      cat "${RAW_DATASET_FIXTURE_JSON:?}"
    else
      printf 'unexpected bq dataset show target: %s\n' "$4" >&2
      exit 99
    fi
    ;;
  "ls --format=prettyjson")
    if [[ "$3" == "mim-prod-123456:mim_billing_export" ]]; then
      cat "${RAW_TABLES_FIXTURE_JSON:?}"
    else
      printf 'unexpected bq ls target: %s\n' "$3" >&2
      exit 99
    fi
    ;;
  "get-iam-policy --table=true")
    if [[ "$4" == "mim-prod-123456:mim_billing_export.gcp_billing_export_resource_v1_01F00BAR" ]]; then
      cat "${RAW_TABLE_IAM_FIXTURE_JSON:?}"
    elif [[ "$4" == "mim-prod-123456:mim_billing_secure.mim_usage_costs_v1" ]]; then
      if [[ "${SECURE_VIEW_EXISTS:-false}" == "true" ]]; then
        cat "${VIEW_IAM_FIXTURE_JSON:?}"
      else
        printf 'NOT_FOUND: Table mim-prod-123456:mim_billing_secure.mim_usage_costs_v1 was not found\n' >&2
        exit 1
      fi
    else
      printf 'unexpected bq get-iam-policy target: %s\n' "$4" >&2
      exit 99
    fi
    ;;
  *)
    printf 'unexpected bq invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/bq"

cat >"$TMP_DIR/project-iam.json" <<'EOF'
{"bindings":[{"role":"roles/bigquery.jobUser","members":["serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"]}]}
EOF
cat >"$TMP_DIR/raw-dataset.json" <<'EOF'
{"datasetReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_export"},"location":"asia-northeast3","access":[]}
EOF
cat >"$TMP_DIR/raw-tables.json" <<'EOF'
[{"tableReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_export","tableId":"gcp_billing_export_resource_v1_01F00BAR"},"type":"TABLE"}]
EOF
cat >"$TMP_DIR/raw-table-iam.json" <<'EOF'
{"bindings":[]}
EOF
cat >"$TMP_DIR/secure-dataset.json" <<'EOF'
{"datasetReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_secure"},"location":"asia-northeast3","access":[]}
EOF
cat >"$TMP_DIR/secure-view.json" <<'EOF'
{"tableReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_secure","tableId":"mim_usage_costs_v1"},"type":"VIEW","view":{"query":"stale"}}
EOF
cat >"$TMP_DIR/view-iam.json" <<'EOF'
{"bindings":[]}
EOF

PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/plan.out" 2>&1

[[ -f "$PLAN_PATH" ]] || { printf 'FAIL ready_plan: missing plan file\n' >&2; exit 1; }
[[ -f "$PLAN_PATH.sha256" ]] || { printf 'FAIL ready_plan: missing plan hash\n' >&2; exit 1; }
[[ "$(stat -f '%Lp' "$PLAN_PATH")" == "600" ]] || { printf 'FAIL ready_plan: wrong plan mode\n' >&2; exit 1; }

billing_assert_contains "$TMP_DIR/plan.out" "Wrote reviewed plan" ready_plan
billing_assert_contains "$GCLOUD_LOG" "--account=$BILLING_OPERATOR_EMAIL" ready_plan
billing_assert_contains "$BQ_LOG" "account=$BILLING_OPERATOR_EMAIL project=$BILLING_PROJECT_ID" ready_plan
billing_assert_contains "$BQ_LOG" "get-iam-policy --table=true --format=prettyjson mim-prod-123456:mim_billing_export.gcp_billing_export_resource_v1_01F00BAR" ready_plan

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["operator_email"] == "operator.test@madup.com"
assert plan["constraints"]["mutation_boundary"] == "central_operator_only"
assert plan["resource"]["project_id"] == "mim-prod-123456"
assert plan["resource"]["raw_dataset_id"] == "mim_billing_export"
assert plan["resource"]["secure_dataset_id"] == "mim_billing_secure"
assert plan["resource"]["secure_view_id"] == "mim_usage_costs_v1"
assert plan["resource"]["raw_table_id"] == "gcp_billing_export_resource_v1_01F00BAR"
assert "gcp_billing_export_resource_v1_01F00BAR" in plan["resource"]["view_query"]
assert "*" not in plan["resource"]["view_query"]
assert "mim-prod-123456.mim_billing_export.gcp_billing_export_resource_v1_*" not in plan["resource"]["view_query"]
action_kinds = [action["kind"] for action in plan["actions"]]
assert action_kinds == [
    "upsert_secure_view",
    "authorize_secure_view_on_raw_dataset",
    "grant_maintenance_viewer_on_secure_view",
]
for action in plan["actions"]:
    assert action.get("role") != "roles/bigquery.dataViewer" or action.get("resource_kind") != "raw_dataset"
PY

cat >"$TMP_DIR/project-iam-no-job-user.json" <<'EOF'
{"bindings":[]}
EOF
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam-no-job-user.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/blocked.out" 2>&1

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
messages = [item["message"] for item in plan["blockers"]]
assert any("roles/bigquery.jobUser" in message for message in messages)
PY

cat >"$TMP_DIR/raw-table-iam-direct-reader.json" <<'EOF'
{"bindings":[{"role":"roles/bigquery.dataViewer","members":["serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"]}]}
EOF
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam-direct-reader.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/raw-table-iam-blocked.out" 2>&1

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
messages = [item["message"] for item in plan["blockers"]]
assert any("raw billing export table" in message for message in messages)
PY

cat >"$TMP_DIR/project-iam-unexpected-role.json" <<'EOF'
{"bindings":[{"role":"roles/bigquery.jobUser","members":["serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"]},{"role":"roles/bigquery.dataViewer","members":["serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"]}]}
EOF
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam-unexpected-role.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/project-role-blocked.out" 2>&1

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
messages = [item["message"] for item in plan["blockers"]]
assert any("project role" in message for message in messages)
PY

cat >"$TMP_DIR/raw-dataset-missing-location.json" <<'EOF'
{"datasetReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_export"},"access":[]}
EOF
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset-missing-location.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
SECURE_DATASET_EXISTS=false \
SECURE_VIEW_EXISTS=false \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/missing-location-blocked.out" 2>&1

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
messages = [item["message"] for item in plan["blockers"]]
assert any("dataset location" in message for message in messages)
assert not any(action["kind"] == "create_secure_dataset" for action in plan["actions"])
PY

rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
if PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
SECURE_DATASET_EXISTS=false \
SECURE_VIEW_EXISTS=false \
SECURE_DATASET_ERROR_MODE=host_not_found \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/noncanonical-error.out" 2>&1; then
  printf 'FAIL noncanonical_error: plan unexpectedly succeeded\n' >&2
  exit 1
fi

billing_assert_contains "$TMP_DIR/noncanonical-error.out" "Unable to inspect reviewed BigQuery billing state" noncanonical_error
billing_assert_not_contains "$TMP_DIR/noncanonical-error.out" "lookup bigquery.googleapis.com" noncanonical_error

printf 'PASS test_plan.sh\n'
