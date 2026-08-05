#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"

readonly BILLING_PROJECT_ID='mim-prod-123456'
readonly BILLING_RAW_DATASET='mim_billing_export'
readonly BILLING_SECURE_DATASET='mim_billing_secure'
readonly BILLING_SECURE_VIEW='mim_usage_costs_v1'

CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"
PLAN_FILE=
MODE=
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

billing_optional_json() {
  local output_file=$1
  local resource_kind=$2
  local resource_name=$3
  shift
  shift
  shift

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

  if [[ "$resource_kind" == "dataset" ]] && \
     grep -Fqx -- "NOT_FOUND: Dataset $resource_name was not found" "$stderr_file"; then
    rm -f -- "$stderr_file"
    printf '{}\n' >"$output_file"
    return
  fi

  if [[ "$resource_kind" == "view" ]] && \
     grep -Fqx -- "NOT_FOUND: Table $resource_name was not found" "$stderr_file"; then
    rm -f -- "$stderr_file"
    printf '{}\n' >"$output_file"
    return
  fi

  rm -f -- "$stderr_file"
  mim_task18_fail "Unable to inspect reviewed BigQuery billing state."
}

billing_bq() {
  CLOUDSDK_CORE_ACCOUNT="$OPERATOR_EMAIL" \
  CLOUDSDK_CORE_PROJECT="$BILLING_PROJECT_ID" \
  bq "$@"
}

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

TMP_DIR=$(mktemp -d)
SNAPSHOT_DIR=$(mktemp -d)
EXPECTED_PATH="$SCRIPT_DIR/.state/billing-expected-$$.json"
trap 'rm -rf "$TMP_DIR" "$SNAPSHOT_DIR"; rm -f "$EXPECTED_PATH" "$EXPECTED_PATH.sha256"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v gcloud >/dev/null 2>&1 || mim_task18_fail "gcloud CLI is required"
command -v bq >/dev/null 2>&1 || mim_task18_fail "bq CLI is required"
[[ "$MIM_PROJECT_ID" == "$BILLING_PROJECT_ID" ]] || mim_task18_fail "Billing surface is pinned to mim-prod-123456"
OPERATOR_EMAIL=$(mim_task18_assert_active_gcloud_account)

MIM_CONFIG_FILE="$SNAPSHOT_CONFIG" \
MIM_BILLING_PLAN_GENERATED_AT="$PLAN_GENERATED_AT" \
MIM_BILLING_PLAN_EXPIRES_AT="$PLAN_EXPIRES_AT" \
bash "$SCRIPT_DIR/plan.sh" --plan --out "$EXPECTED_PATH" >/dev/null
comparison=$(mim_task18_compare_plans "$PLAN_FILE" "$EXPECTED_PATH")
case "$comparison" in
  ok) ;;
  drift) mim_task18_fail "Discovery drift detected" ;;
  *) mim_task18_fail "Plan file does not match the expected reviewed contract" ;;
esac

status=$(python3 - "$PLAN_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["status"])
PY
)
[[ "$status" == "ready" ]] || mim_task18_fail "Reviewed plan contains blockers"

python3 - "$PLAN_FILE" <<'PY' >"$TMP_DIR/actions.tsv"
import base64
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
resource = plan["resource"]
print(
    "resource\t"
    f"{resource['raw_dataset_id']}\t"
    f"{resource['raw_table_id'] or ''}\t"
    f"{resource['secure_dataset_id']}\t"
    f"{resource['secure_view_id']}"
)
for action in plan["actions"]:
    kind = action["kind"]
    if kind == "create_secure_dataset":
        print(f"{kind}\t{action['dataset_id']}\t{action['location']}")
    elif kind == "upsert_secure_view":
        encoded_query = base64.b64encode(action["view_query"].encode("utf-8")).decode("ascii")
        print(f"{kind}\t{action['dataset_id']}\t{action['view_id']}\t{encoded_query}")
    elif kind == "authorize_secure_view_on_raw_dataset":
        print(f"{kind}\t{action['dataset_id']}\t{action['secure_dataset_id']}\t{action['secure_view_id']}")
    elif kind == "grant_maintenance_viewer_on_secure_view":
        print(f"{kind}\t{action['dataset_id']}\t{action['view_id']}\t{action['member']}\t{action['role']}")
PY

authorize_secure_view_acl() {
  local raw_dataset_ref=$1
  local secure_project_id=$2
  local secure_dataset_id=$3
  local secure_view_id=$4
  local dataset_json=$5
  local updated_json=$6

  python3 - "$dataset_json" "$updated_json" "$secure_project_id" "$secure_dataset_id" "$secure_view_id" <<'PY'
import json
import sys
from pathlib import Path

source = json.loads(Path(sys.argv[1]).read_text())
access = source.get("access")
if not isinstance(access, list):
    access = []
entry = {
    "view": {
        "projectId": sys.argv[3],
        "datasetId": sys.argv[4],
        "tableId": sys.argv[5],
    }
}
for existing in access:
    if existing == entry:
        break
else:
    access.append(entry)
source["access"] = access
Path(sys.argv[2]).write_text(json.dumps(source, indent=2, sort_keys=True) + "\n")
PY

  billing_bq update --source "$updated_json" --update_mode=UPDATE_ACL "$raw_dataset_ref"
}

while IFS=$'\t' read -r kind a b c d; do
  [[ -n "$kind" ]] || continue
  case "$kind" in
    resource)
      RAW_DATASET_ID="$a"
      RAW_TABLE_ID="$b"
      SECURE_DATASET_ID="$c"
      SECURE_VIEW_ID="$d"
      ;;
    create_secure_dataset)
      billing_bq mk --dataset --location="$b" "$BILLING_PROJECT_ID:$a"
      ;;
    upsert_secure_view)
      view_query=$(python3 - "$c" <<'PY'
import base64
import sys

print(base64.b64decode(sys.argv[1]).decode("utf-8"))
PY
      )
      CURRENT_VIEW_JSON="$TMP_DIR/current-view.json"
      billing_optional_json "$CURRENT_VIEW_JSON" view "$BILLING_PROJECT_ID:$a.$b" \
        billing_bq show --format=prettyjson "$BILLING_PROJECT_ID:$a.$b"
      if python3 - "$CURRENT_VIEW_JSON" <<'PY' >/dev/null
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
raise SystemExit(0 if payload.get("tableReference") or payload.get("table_reference") else 1)
PY
      then
        billing_bq update --use_legacy_sql=false --view "$view_query" "$BILLING_PROJECT_ID:$a.$b"
      else
        billing_bq mk --use_legacy_sql=false --view "$view_query" "$BILLING_PROJECT_ID:$a.$b"
      fi
      ;;
    authorize_secure_view_on_raw_dataset)
      CURRENT_DATASET_JSON="$TMP_DIR/raw-dataset.json"
      UPDATED_DATASET_JSON="$TMP_DIR/raw-dataset-updated.json"
      billing_bq show --dataset_view=FULL --format=prettyjson "$BILLING_PROJECT_ID:$a" >"$CURRENT_DATASET_JSON"
      authorize_secure_view_acl \
        "$BILLING_PROJECT_ID:$a" \
        "$BILLING_PROJECT_ID" \
        "$b" \
        "$c" \
        "$CURRENT_DATASET_JSON" \
        "$UPDATED_DATASET_JSON"
      ;;
    grant_maintenance_viewer_on_secure_view)
      billing_bq add-iam-policy-binding --member="$c" --role="$d" --table=true "$BILLING_PROJECT_ID:$a.$b"
      ;;
    *)
      mim_task18_fail "Unknown reviewed action: $kind"
      ;;
  esac
done <"$TMP_DIR/actions.tsv"

RAW_READBACK_JSON="$TMP_DIR/raw-readback.json"
RAW_TABLE_IAM_READBACK_JSON="$TMP_DIR/raw-table-iam-readback.json"
VIEW_READBACK_JSON="$TMP_DIR/view-readback.json"
VIEW_IAM_READBACK_JSON="$TMP_DIR/view-iam-readback.json"
billing_bq show --dataset_view=FULL --format=prettyjson "$BILLING_PROJECT_ID:$RAW_DATASET_ID" >"$RAW_READBACK_JSON"
if [[ -z "${RAW_TABLE_ID:-}" ]]; then
  mim_task18_fail "Plan file does not match the expected reviewed contract"
fi
billing_bq get-iam-policy --table=true --format=prettyjson "$BILLING_PROJECT_ID:$RAW_DATASET_ID.$RAW_TABLE_ID" >"$RAW_TABLE_IAM_READBACK_JSON"
billing_bq show --format=prettyjson "$BILLING_PROJECT_ID:$SECURE_DATASET_ID.$SECURE_VIEW_ID" >"$VIEW_READBACK_JSON"
billing_bq get-iam-policy --table=true --format=prettyjson "$BILLING_PROJECT_ID:$SECURE_DATASET_ID.$SECURE_VIEW_ID" >"$VIEW_IAM_READBACK_JSON"

python3 - "$PLAN_FILE" "$RAW_READBACK_JSON" "$RAW_TABLE_IAM_READBACK_JSON" "$VIEW_READBACK_JSON" "$VIEW_IAM_READBACK_JSON" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
raw_dataset = json.loads(Path(sys.argv[2]).read_text())
raw_table_iam = json.loads(Path(sys.argv[3]).read_text())
view = json.loads(Path(sys.argv[4]).read_text())
view_iam = json.loads(Path(sys.argv[5]).read_text())

resource = plan["resource"]
expected_view = {
    "projectId": resource["project_id"],
    "datasetId": resource["secure_dataset_id"],
    "tableId": resource["secure_view_id"],
}

def raw_has_view():
    for entry in raw_dataset.get("access") or []:
        view_entry = entry.get("view")
        if view_entry == expected_view:
            return True
    return False

if not raw_has_view():
    raise SystemExit("Raw dataset readback is missing the authorized view entry")

member = "serviceAccount:mim-maintenance@mim-prod-123456.iam.gserviceaccount.com"
forbidden_roles = {
    "roles/bigquery.dataOwner",
    "roles/bigquery.dataEditor",
    "roles/bigquery.dataViewer",
    "roles/bigquery.filteredDataViewer",
}
for binding in raw_table_iam.get("bindings") or []:
    if binding.get("role") in forbidden_roles and member in (binding.get("members") or []):
        raise SystemExit("Maintenance service account must not retain direct reader access on the raw billing export table")

view_query = ((view.get("view") or {}).get("query"))
if view_query != resource["view_query"]:
    raise SystemExit("Secure billing view readback drifted from the reviewed query")

for binding in view_iam.get("bindings") or []:
    if binding.get("role") == "roles/bigquery.dataViewer" and member in (binding.get("members") or []):
        break
else:
    raise SystemExit("Secure billing view IAM readback is missing the maintenance viewer binding")
PY

printf 'Applied reviewed plan.\n'
