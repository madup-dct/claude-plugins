#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/test_common.sh"

PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
APPLY_SCRIPT="$SCRIPT_DIR/apply.sh"
TMP_DIR=$(mktemp -d)
STATE_DIR="$SCRIPT_DIR/.state"
PLAN_PATH="$STATE_DIR/test-billing-apply-$$.json"
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
        if [[ -f "${RAW_ACL_MARKER:?}" ]]; then
          cat "${RAW_DATASET_AUTHORIZED_FIXTURE_JSON:?}"
        else
          cat "${RAW_DATASET_FIXTURE_JSON:?}"
        fi
        ;;
      mim-prod-123456:mim_billing_secure)
        if [[ -f "${SECURE_DATASET_MARKER:?}" ]]; then
          cat "${SECURE_DATASET_FIXTURE_JSON:?}"
        else
          printf 'NOT_FOUND: dataset missing\n' >&2
          exit 1
        fi
        ;;
      mim-prod-123456:mim_billing_secure.mim_usage_costs_v1)
        if [[ "${APPLY_SECURE_VIEW_ERROR_MODE:-ok}" == "host_not_found" ]]; then
          printf 'lookup bigquery.googleapis.com: no such host\n' >&2
          exit 1
        elif [[ -f "${VIEW_MARKER:?}" ]]; then
          cat "${SECURE_VIEW_READY_FIXTURE_JSON:?}"
        elif [[ "${SECURE_VIEW_EXISTS:-false}" == "true" ]]; then
          cat "${SECURE_VIEW_FIXTURE_JSON:?}"
        else
          printf 'NOT_FOUND: view missing\n' >&2
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
      if [[ -f "${RAW_ACL_MARKER:?}" ]]; then
        cat "${RAW_DATASET_AUTHORIZED_FIXTURE_JSON:?}"
      else
        cat "${RAW_DATASET_FIXTURE_JSON:?}"
      fi
    else
      printf 'unexpected dataset show target: %s\n' "$4" >&2
      exit 99
    fi
    ;;
  "ls --format=prettyjson")
    cat "${RAW_TABLES_FIXTURE_JSON:?}"
    ;;
  "get-iam-policy --table=true")
    if [[ "$4" == "mim-prod-123456:mim_billing_export.gcp_billing_export_resource_v1_01F00BAR" ]]; then
      count_file="${RAW_TABLE_IAM_CALL_COUNT_FILE:?}"
      count=0
      if [[ -f "$count_file" ]]; then
        count=$(cat "$count_file")
      fi
      count=$((count + 1))
      printf '%s' "$count" >"$count_file"
      if [[ "$count" -ge 2 && "${RAW_TABLE_IAM_READBACK_MODE:-clean}" == "direct_reader" ]]; then
        cat "${RAW_TABLE_IAM_BAD_FIXTURE_JSON:?}"
      else
        cat "${RAW_TABLE_IAM_FIXTURE_JSON:?}"
      fi
    elif [[ "$4" == "mim-prod-123456:mim_billing_secure.mim_usage_costs_v1" ]]; then
      if [[ -f "${VIEW_IAM_MARKER:?}" ]]; then
        cat "${VIEW_IAM_READY_FIXTURE_JSON:?}"
      elif [[ "${SECURE_VIEW_EXISTS:-false}" == "true" ]]; then
        cat "${VIEW_IAM_FIXTURE_JSON:?}"
      else
        printf 'NOT_FOUND: Table mim-prod-123456:mim_billing_secure.mim_usage_costs_v1 was not found\n' >&2
        exit 1
      fi
    else
      printf 'unexpected get-iam-policy target: %s\n' "$4" >&2
      exit 99
    fi
    ;;
  "mk --dataset")
    : > "${SECURE_DATASET_MARKER:?}"
    ;;
  "mk --use_legacy_sql=false")
    : > "${VIEW_MARKER:?}"
    ;;
  "update --use_legacy_sql=false")
    : > "${VIEW_MARKER:?}"
    ;;
  "update --source")
    : > "${RAW_ACL_MARKER:?}"
    ;;
  "add-iam-policy-binding --member=serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com")
    : > "${VIEW_IAM_MARKER:?}"
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
cat >"$TMP_DIR/raw-dataset-authorized.json" <<'EOF'
{"datasetReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_export"},"location":"asia-northeast3","access":[{"view":{"projectId":"mim-prod-123456","datasetId":"mim_billing_secure","tableId":"mim_usage_costs_v1"}}]}
EOF
cat >"$TMP_DIR/raw-tables.json" <<'EOF'
[{"tableReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_export","tableId":"gcp_billing_export_resource_v1_01F00BAR"},"type":"TABLE"}]
EOF
cat >"$TMP_DIR/raw-table-iam.json" <<'EOF'
{"bindings":[]}
EOF
cat >"$TMP_DIR/raw-table-iam-bad.json" <<'EOF'
{"bindings":[{"role":"roles/bigquery.dataViewer","members":["serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"]}]}
EOF
cat >"$TMP_DIR/secure-dataset.json" <<'EOF'
{"datasetReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_secure"},"location":"asia-northeast3","access":[]}
EOF
cat >"$TMP_DIR/secure-view.json" <<'EOF'
{"tableReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_secure","tableId":"mim_usage_costs_v1"},"type":"VIEW","view":{"query":"stale"}}
EOF
cat >"$TMP_DIR/secure-view-ready.json" <<'EOF'
{"tableReference":{"projectId":"mim-prod-123456","datasetId":"mim_billing_secure","tableId":"mim_usage_costs_v1"},"type":"VIEW","view":{"query":"SELECT\n  invoice.month AS invoice_month,\n  service.description AS service_description,\n  currency AS currency,\n  (\n    SELECT label.value\n    FROM UNNEST(labels) AS label\n    WHERE label.key = 'owner-hash'\n  ) AS owner_hash,\n  (\n    SELECT label.value\n    FROM UNNEST(labels) AS label\n    WHERE label.key = 'workload-hash'\n  ) AS workload_hash,\n  CAST(\n    ROUND(\n      SUM(\n        cost\n        + IFNULL(\n            (SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit),\n            0\n          )\n      ),\n      0\n    ) AS INT64\n  ) AS measured_cost_krw,\n  FALSE AS source_finalized\nFROM `mim-prod-123456.mim_billing_export.gcp_billing_export_resource_v1_01F00BAR`\nWHERE project.id = 'mim-prod-123456'\n  AND EXISTS (\n    SELECT 1\n    FROM UNNEST(labels) AS label\n    WHERE label.key = 'managed-by'\n      AND label.value = 'mim-control-plane'\n  )\nGROUP BY\n  invoice_month,\n  service_description,\n  currency,\n  owner_hash,\n  workload_hash,\n  source_finalized\n"}}
EOF
cat >"$TMP_DIR/view-iam.json" <<'EOF'
{"bindings":[]}
EOF
cat >"$TMP_DIR/view-iam-ready.json" <<'EOF'
{"bindings":[{"role":"roles/bigquery.dataViewer","members":["serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"]}]}
EOF

RAW_ACL_MARKER="$TMP_DIR/raw-acl.marker"
SECURE_DATASET_MARKER="$TMP_DIR/secure-dataset.marker"
VIEW_MARKER="$TMP_DIR/view.marker"
VIEW_IAM_MARKER="$TMP_DIR/view-iam.marker"
RAW_TABLE_IAM_CALL_COUNT_FILE="$TMP_DIR/raw-table-iam.calls"
: >"$SECURE_DATASET_MARKER"

PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/plan.out" 2>&1

: >"$RAW_TABLE_IAM_CALL_COUNT_FILE"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/apply.out" 2>&1

billing_assert_contains "$TMP_DIR/apply.out" "Applied reviewed plan." apply_success
billing_assert_contains "$BQ_LOG" "account=$BILLING_OPERATOR_EMAIL project=$BILLING_PROJECT_ID" apply_success
billing_assert_contains "$BQ_LOG" "update --source" apply_success
billing_assert_contains "$BQ_LOG" "add-iam-policy-binding --member=serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com --role=roles/bigquery.dataViewer --table=true mim-prod-123456:mim_billing_secure.mim_usage_costs_v1" apply_success
billing_assert_not_contains "$BQ_LOG" "roles/bigquery.dataViewer mim-prod-123456:mim_billing_export" apply_success
billing_assert_contains "$BQ_LOG" "get-iam-policy --table=true --format=prettyjson mim-prod-123456:mim_billing_export.gcp_billing_export_resource_v1_01F00BAR" apply_success
[[ "$(cat "$RAW_TABLE_IAM_CALL_COUNT_FILE")" -ge 2 ]] || { printf 'FAIL apply_success: raw table IAM was not re-read during apply\n' >&2; exit 1; }

python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
plan["resource"]["secure_view_id"] = "tampered"
Path(sys.argv[1]).write_text(json.dumps(plan))
PY

if PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/tamper.out" 2>&1; then
  printf 'FAIL tamper_detection: apply unexpectedly succeeded\n' >&2
  exit 1
fi

billing_assert_contains "$TMP_DIR/tamper.out" "Plan hash verification failed" tamper_detection

rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" "$RAW_ACL_MARKER" "$VIEW_MARKER" "$VIEW_IAM_MARKER"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/drift-plan.out" 2>&1

cp "$TMP_DIR/raw-table-iam-bad.json" "$TMP_DIR/raw-table-iam.json"
if PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/raw-table-drift.out" 2>&1; then
  printf 'FAIL raw_table_drift: apply unexpectedly succeeded\n' >&2
  exit 1
fi

billing_assert_contains "$TMP_DIR/raw-table-drift.out" "Discovery drift detected" raw_table_drift

cp "$TMP_DIR/raw-table-iam.json" "$TMP_DIR/raw-table-iam.orig.json"
cat >"$TMP_DIR/raw-table-iam.json" <<'EOF'
{"bindings":[]}
EOF
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" "$RAW_ACL_MARKER" "$VIEW_MARKER" "$VIEW_IAM_MARKER"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/readback-plan.out" 2>&1

: >"$RAW_TABLE_IAM_CALL_COUNT_FILE"
if PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
RAW_TABLE_IAM_READBACK_MODE=direct_reader \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/raw-table-readback.out" 2>&1; then
  printf 'FAIL raw_table_readback: apply unexpectedly succeeded\n' >&2
  exit 1
fi

billing_assert_contains "$TMP_DIR/raw-table-readback.out" "raw billing export table" raw_table_readback

cat >"$TMP_DIR/raw-table-iam.json" <<'EOF'
{"bindings":[]}
EOF
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256" "$RAW_ACL_MARKER" "$VIEW_MARKER" "$VIEW_IAM_MARKER"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$TMP_DIR/noncanonical-plan.out" 2>&1

if PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
BQ_LOG="$BQ_LOG" \
PROJECT_IAM_FIXTURE_JSON="$TMP_DIR/project-iam.json" \
RAW_DATASET_FIXTURE_JSON="$TMP_DIR/raw-dataset.json" \
RAW_DATASET_AUTHORIZED_FIXTURE_JSON="$TMP_DIR/raw-dataset-authorized.json" \
RAW_TABLES_FIXTURE_JSON="$TMP_DIR/raw-tables.json" \
RAW_TABLE_IAM_FIXTURE_JSON="$TMP_DIR/raw-table-iam.json" \
RAW_TABLE_IAM_BAD_FIXTURE_JSON="$TMP_DIR/raw-table-iam-bad.json" \
SECURE_DATASET_FIXTURE_JSON="$TMP_DIR/secure-dataset.json" \
SECURE_VIEW_FIXTURE_JSON="$TMP_DIR/secure-view.json" \
SECURE_VIEW_READY_FIXTURE_JSON="$TMP_DIR/secure-view-ready.json" \
VIEW_IAM_FIXTURE_JSON="$TMP_DIR/view-iam.json" \
VIEW_IAM_READY_FIXTURE_JSON="$TMP_DIR/view-iam-ready.json" \
RAW_ACL_MARKER="$RAW_ACL_MARKER" \
SECURE_DATASET_MARKER="$SECURE_DATASET_MARKER" \
VIEW_MARKER="$VIEW_MARKER" \
VIEW_IAM_MARKER="$VIEW_IAM_MARKER" \
RAW_TABLE_IAM_CALL_COUNT_FILE="$RAW_TABLE_IAM_CALL_COUNT_FILE" \
APPLY_SECURE_VIEW_ERROR_MODE=host_not_found \
SECURE_DATASET_EXISTS=true \
SECURE_VIEW_EXISTS=true \
MIM_CONFIG_FILE="$CONFIG_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/noncanonical-apply.out" 2>&1; then
  printf 'FAIL noncanonical_apply_error: apply unexpectedly succeeded\n' >&2
  exit 1
fi

billing_assert_contains "$TMP_DIR/noncanonical-apply.out" "Unable to inspect reviewed BigQuery billing state" noncanonical_apply_error
billing_assert_not_contains "$TMP_DIR/noncanonical-apply.out" "lookup bigquery.googleapis.com" noncanonical_apply_error

printf 'PASS test_apply.sh\n'
