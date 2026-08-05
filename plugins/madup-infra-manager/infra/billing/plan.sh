#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"

readonly BILLING_PROJECT_ID='mim-prod-123456'
readonly BILLING_RAW_DATASET='mim_billing_export'
readonly BILLING_RAW_TABLE_PREFIX='gcp_billing_export_resource_v1_'
readonly BILLING_SECURE_DATASET='mim_billing_secure'
readonly BILLING_SECURE_VIEW='mim_usage_costs_v1'
readonly BILLING_MANAGED_BY_KEY='managed-by'
readonly BILLING_MANAGED_BY_VALUE='mim-control-plane'
readonly BILLING_OWNER_HASH_KEY='owner-hash'
readonly BILLING_WORKLOAD_HASH_KEY='workload-hash'
readonly BILLING_MUTATION_BOUNDARY='central_operator_only'

CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"
MODE=
PLAN_OUT=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --plan) MODE=plan; shift ;;
    --out) PLAN_OUT=$2; shift 2 ;;
    --*) mim_task18_fail "Unknown argument: $1" ;;
    *) mim_task18_fail "Positional arguments are not supported" ;;
  esac
done
[[ "$MODE" == plan && -n "$PLAN_OUT" ]] || mim_task18_fail "Usage: plan.sh --plan --out .state/<name>.json"
mim_task18_assert_plan_create_path "$SCRIPT_DIR" "$PLAN_OUT"

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

build_view_query() {
  local raw_table_fqn=$1
  python3 - "$raw_table_fqn" <<'PY'
import sys

raw_table_fqn = sys.argv[1]
print(
    "SELECT\n"
    "  invoice.month AS invoice_month,\n"
    "  service.description AS service_description,\n"
    "  currency AS currency,\n"
    "  (\n"
    "    SELECT label.value\n"
    "    FROM UNNEST(labels) AS label\n"
    "    WHERE label.key = 'owner-hash'\n"
    "  ) AS owner_hash,\n"
    "  (\n"
    "    SELECT label.value\n"
    "    FROM UNNEST(labels) AS label\n"
    "    WHERE label.key = 'workload-hash'\n"
    "  ) AS workload_hash,\n"
    "  CAST(\n"
    "    ROUND(\n"
    "      SUM(\n"
    "        cost\n"
    "        + IFNULL(\n"
    "            (SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit),\n"
    "            0\n"
    "          )\n"
    "      ),\n"
    "      0\n"
    "    ) AS INT64\n"
    "  ) AS measured_cost_krw,\n"
    "  FALSE AS source_finalized\n"
    f"FROM `{raw_table_fqn}`\n"
    "WHERE project.id = 'mim-prod-123456'\n"
    "  AND EXISTS (\n"
    "    SELECT 1\n"
    "    FROM UNNEST(labels) AS label\n"
    "    WHERE label.key = 'managed-by'\n"
    "      AND label.value = 'mim-control-plane'\n"
    "  )\n"
    "GROUP BY\n"
    "  invoice_month,\n"
    "  service_description,\n"
    "  currency,\n"
    "  owner_hash,\n"
    "  workload_hash,\n"
    "  source_finalized\n"
)
PY
}

TMP_DIR=$(mktemp -d)
SNAPSHOT_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR" "$SNAPSHOT_DIR"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v gcloud >/dev/null 2>&1 || mim_task18_fail "gcloud CLI is required"
command -v bq >/dev/null 2>&1 || mim_task18_fail "bq CLI is required"
[[ "$MIM_PROJECT_ID" == "$BILLING_PROJECT_ID" ]] || mim_task18_fail "Billing surface is pinned to mim-prod-123456"
OPERATOR_EMAIL=$(mim_task18_assert_active_gcloud_account)

PROJECT_IAM_JSON="$TMP_DIR/project-iam.json"
RAW_DATASET_JSON="$TMP_DIR/raw-dataset.json"
RAW_TABLES_JSON="$TMP_DIR/raw-tables.json"
RAW_TABLE_IAM_JSON="$TMP_DIR/raw-table-iam.json"
SECURE_DATASET_JSON="$TMP_DIR/secure-dataset.json"
SECURE_VIEW_JSON="$TMP_DIR/secure-view.json"
VIEW_IAM_JSON="$TMP_DIR/view-iam.json"

gcloud projects get-iam-policy "$BILLING_PROJECT_ID" \
  '--format=json' \
  --account="$OPERATOR_EMAIL" \
  --project="$BILLING_PROJECT_ID" >"$PROJECT_IAM_JSON"

billing_bq show --dataset_view=FULL --format=prettyjson \
  "$BILLING_PROJECT_ID:$BILLING_RAW_DATASET" >"$RAW_DATASET_JSON"
billing_bq ls --format=prettyjson "$BILLING_PROJECT_ID:$BILLING_RAW_DATASET" >"$RAW_TABLES_JSON"
RAW_TABLE_ID=$(python3 - "$RAW_TABLES_JSON" "$BILLING_RAW_TABLE_PREFIX" <<'PY'
import json
import sys
from pathlib import Path

items = json.loads(Path(sys.argv[1]).read_text())
prefix = sys.argv[2]
matches = []
for item in items:
    ref = item.get("tableReference") or item.get("table_reference") or {}
    table_id = ref.get("tableId") or ref.get("table_id")
    if isinstance(table_id, str) and table_id.startswith(prefix) and item.get("type") == "TABLE":
        matches.append(table_id)
if len(matches) == 1:
    print(matches[0])
PY
)
if [[ -n "$RAW_TABLE_ID" ]]; then
  billing_bq get-iam-policy --table=true --format=prettyjson \
    "$BILLING_PROJECT_ID:$BILLING_RAW_DATASET.$RAW_TABLE_ID" >"$RAW_TABLE_IAM_JSON"
else
  printf '{}\n' >"$RAW_TABLE_IAM_JSON"
fi
billing_optional_json "$SECURE_DATASET_JSON" dataset "$BILLING_PROJECT_ID:$BILLING_SECURE_DATASET" \
  billing_bq show --format=prettyjson "$BILLING_PROJECT_ID:$BILLING_SECURE_DATASET"
billing_optional_json "$SECURE_VIEW_JSON" view "$BILLING_PROJECT_ID:$BILLING_SECURE_DATASET.$BILLING_SECURE_VIEW" \
  billing_bq show --format=prettyjson "$BILLING_PROJECT_ID:$BILLING_SECURE_DATASET.$BILLING_SECURE_VIEW"
billing_optional_json "$VIEW_IAM_JSON" view "$BILLING_PROJECT_ID:$BILLING_SECURE_DATASET.$BILLING_SECURE_VIEW" \
  billing_bq get-iam-policy --table=true --format=prettyjson \
  "$BILLING_PROJECT_ID:$BILLING_SECURE_DATASET.$BILLING_SECURE_VIEW"

GENERATED_AT="${MIM_BILLING_PLAN_GENERATED_AT:-$(mim_task18_now_epoch)}"
EXPIRES_AT="${MIM_BILLING_PLAN_EXPIRES_AT:-$((GENERATED_AT + MIM_TASK18_PLAN_MAX_AGE_SECONDS))}"
CONFIG_FINGERPRINT=$(mim_task18_config_fingerprint "$SNAPSHOT_CONFIG")
PLAN_TMP="$TMP_DIR/billing-plan.json"
MAINTENANCE_EMAIL="mim-maintenance@$BILLING_PROJECT_ID.iam.gserviceaccount.com"

PLAN_GENERATED_AT="$GENERATED_AT" \
PLAN_EXPIRES_AT="$EXPIRES_AT" \
PLAN_CONFIG_FINGERPRINT="$CONFIG_FINGERPRINT" \
PLAN_OPERATOR_EMAIL="$OPERATOR_EMAIL" \
PLAN_PROJECT_ID="$BILLING_PROJECT_ID" \
PLAN_MAINTENANCE_EMAIL="$MAINTENANCE_EMAIL" \
PLAN_MUTATION_BOUNDARY="$BILLING_MUTATION_BOUNDARY" \
PLAN_RAW_DATASET_JSON="$RAW_DATASET_JSON" \
PLAN_RAW_TABLES_JSON="$RAW_TABLES_JSON" \
PLAN_RAW_TABLE_IAM_JSON="$RAW_TABLE_IAM_JSON" \
PLAN_SECURE_DATASET_JSON="$SECURE_DATASET_JSON" \
PLAN_SECURE_VIEW_JSON="$SECURE_VIEW_JSON" \
PLAN_VIEW_IAM_JSON="$VIEW_IAM_JSON" \
PLAN_PROJECT_IAM_JSON="$PROJECT_IAM_JSON" \
PLAN_RAW_DATASET_ID="$BILLING_RAW_DATASET" \
PLAN_RAW_TABLE_PREFIX="$BILLING_RAW_TABLE_PREFIX" \
PLAN_SECURE_DATASET_ID="$BILLING_SECURE_DATASET" \
PLAN_SECURE_VIEW_ID="$BILLING_SECURE_VIEW" \
python3 - "$PLAN_TMP" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path


def load_json(path_env: str):
    path = Path(os.environ[path_env])
    return json.loads(path.read_text())


def find_raw_table_id(items, prefix: str):
    matches = []
    for item in items:
        ref = item.get("tableReference") or item.get("table_reference") or {}
        table_id = ref.get("tableId") or ref.get("table_id")
        item_type = item.get("type")
        if isinstance(table_id, str) and table_id.startswith(prefix) and item_type == "TABLE":
            matches.append(table_id)
    return sorted(matches)


def access_entries(payload):
    if not isinstance(payload, dict):
        return []
    access = payload.get("access")
    return access if isinstance(access, list) else []


def has_maintenance_raw_access(raw_dataset, maintenance_email: str):
    maintenance_member = f"serviceAccount:{maintenance_email}"
    for entry in access_entries(raw_dataset):
        if not isinstance(entry, dict):
            continue
        if entry.get("userByEmail") == maintenance_email:
            return True
        if entry.get("iamMember") == maintenance_member:
            return True
    return False


def has_project_job_user(project_policy, maintenance_email: str):
    maintenance_member = f"serviceAccount:{maintenance_email}"
    for binding in project_policy.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        if binding.get("role") != "roles/bigquery.jobUser":
            continue
        members = binding.get("members") or []
        if maintenance_member in members:
            return True
    return False


def unexpected_project_bigquery_roles(project_policy, maintenance_email: str):
    maintenance_member = f"serviceAccount:{maintenance_email}"
    roles = []
    for binding in project_policy.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        role = binding.get("role")
        if not isinstance(role, str) or not role.startswith("roles/bigquery."):
            continue
        if role == "roles/bigquery.jobUser":
            continue
        members = binding.get("members") or []
        if maintenance_member in members:
            roles.append(role)
    return sorted(set(roles))


def secure_dataset_exists(payload):
    return isinstance(payload, dict) and bool(payload.get("datasetReference") or payload.get("dataset_reference"))


def secure_view_exists(payload):
    return isinstance(payload, dict) and bool(payload.get("tableReference") or payload.get("table_reference"))


def dataset_location(payload):
    location = payload.get("location")
    return location if isinstance(location, str) and location else None


def current_view_query(payload):
    view = payload.get("view")
    if not isinstance(view, dict):
        return None
    query = view.get("query")
    return query if isinstance(query, str) else None


def has_view_authorization(raw_dataset, project_id: str, dataset_id: str, view_id: str):
    expected = (project_id, dataset_id, view_id)
    for entry in access_entries(raw_dataset):
        if not isinstance(entry, dict):
            continue
        view = entry.get("view")
        if not isinstance(view, dict):
            continue
        candidate = (
            view.get("projectId") or view.get("project_id"),
            view.get("datasetId") or view.get("dataset_id"),
            view.get("tableId") or view.get("table_id"),
        )
        if candidate == expected:
            return True
    return False


def has_viewer_binding(view_iam, maintenance_email: str):
    target = f"serviceAccount:{maintenance_email}"
    for binding in view_iam.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        if binding.get("role") != "roles/bigquery.dataViewer":
            continue
        members = binding.get("members") or []
        if target in members:
            return True
    return False


def has_raw_table_reader_binding(raw_table_iam, maintenance_email: str):
    maintenance_member = f"serviceAccount:{maintenance_email}"
    forbidden_roles = {
        "roles/bigquery.dataOwner",
        "roles/bigquery.dataEditor",
        "roles/bigquery.dataViewer",
        "roles/bigquery.filteredDataViewer",
    }
    for binding in raw_table_iam.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        if binding.get("role") not in forbidden_roles:
            continue
        members = binding.get("members") or []
        if maintenance_member in members:
            return True
    return False


project_id = os.environ["PLAN_PROJECT_ID"]
maintenance_email = os.environ["PLAN_MAINTENANCE_EMAIL"]
raw_dataset_id = os.environ["PLAN_RAW_DATASET_ID"]
raw_table_prefix = os.environ["PLAN_RAW_TABLE_PREFIX"]
secure_dataset_id = os.environ["PLAN_SECURE_DATASET_ID"]
secure_view_id = os.environ["PLAN_SECURE_VIEW_ID"]

raw_dataset = load_json("PLAN_RAW_DATASET_JSON")
raw_tables = load_json("PLAN_RAW_TABLES_JSON")
raw_table_iam = load_json("PLAN_RAW_TABLE_IAM_JSON")
secure_dataset = load_json("PLAN_SECURE_DATASET_JSON")
secure_view = load_json("PLAN_SECURE_VIEW_JSON")
view_iam = load_json("PLAN_VIEW_IAM_JSON")
project_iam = load_json("PLAN_PROJECT_IAM_JSON")

blockers: list[dict[str, str]] = []
actions: list[dict[str, str]] = []
raw_matches = find_raw_table_id(raw_tables, raw_table_prefix)
if len(raw_matches) != 1:
    blockers.append(
        {
            "code": "raw-table-discovery",
            "message": "Expected exactly one raw billing export table matching the fixed prefix.",
        }
    )
    raw_table_id = None
else:
    raw_table_id = raw_matches[0]
    if has_raw_table_reader_binding(raw_table_iam, maintenance_email):
        blockers.append(
            {
                "code": "maintenance-raw-table-access",
                "message": "Maintenance service account must not retain direct reader access on the raw billing export table.",
            }
        )

if not has_project_job_user(project_iam, maintenance_email):
    blockers.append(
        {
            "code": "maintenance-job-user",
            "message": "Maintenance service account must already hold roles/bigquery.jobUser on mim-prod-123456.",
        }
    )

for role in unexpected_project_bigquery_roles(project_iam, maintenance_email):
    blockers.append(
        {
            "code": "maintenance-project-role",
            "message": f"Maintenance service account must not hold unexpected BigQuery project role {role}.",
        }
    )

if has_maintenance_raw_access(raw_dataset, maintenance_email):
    blockers.append(
        {
            "code": "maintenance-raw-dataset-access",
            "message": "Maintenance service account must not retain direct access to the raw billing export dataset.",
        }
    )

raw_location = dataset_location(raw_dataset)
if raw_location is None:
    blockers.append(
        {
            "code": "raw-dataset-location",
            "message": "Raw billing export dataset location must be present before reviewing secure dataset actions.",
        }
    )

secure_location = dataset_location(secure_dataset)
if secure_dataset_exists(secure_dataset) and raw_location and secure_location and raw_location != secure_location:
    blockers.append(
        {
            "code": "secure-dataset-location",
            "message": "Secure billing dataset must stay in the same BigQuery location as the raw export dataset.",
        }
    )

raw_table_fqn = None
view_query = None
if raw_table_id is not None:
    raw_table_fqn = f"{project_id}.{raw_dataset_id}.{raw_table_id}"
    view_query = (
        "SELECT\n"
        "  invoice.month AS invoice_month,\n"
        "  service.description AS service_description,\n"
        "  currency AS currency,\n"
        "  (\n"
        "    SELECT label.value\n"
        "    FROM UNNEST(labels) AS label\n"
        "    WHERE label.key = 'owner-hash'\n"
        "  ) AS owner_hash,\n"
        "  (\n"
        "    SELECT label.value\n"
        "    FROM UNNEST(labels) AS label\n"
        "    WHERE label.key = 'workload-hash'\n"
        "  ) AS workload_hash,\n"
        "  CAST(\n"
        "    ROUND(\n"
        "      SUM(\n"
        "        cost\n"
        "        + IFNULL(\n"
        "            (SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit),\n"
        "            0\n"
        "          )\n"
        "      ),\n"
        "      0\n"
        "    ) AS INT64\n"
        "  ) AS measured_cost_krw,\n"
        "  FALSE AS source_finalized\n"
        f"FROM `{raw_table_fqn}`\n"
        "WHERE project.id = 'mim-prod-123456'\n"
        "  AND EXISTS (\n"
        "    SELECT 1\n"
        "    FROM UNNEST(labels) AS label\n"
        "    WHERE label.key = 'managed-by'\n"
        "      AND label.value = 'mim-control-plane'\n"
        "  )\n"
        "GROUP BY\n"
        "  invoice_month,\n"
        "  service_description,\n"
        "  currency,\n"
        "  owner_hash,\n"
        "  workload_hash,\n"
        "  source_finalized\n"
    )

if not secure_dataset_exists(secure_dataset) and raw_location is not None:
    actions.append(
        {
            "kind": "create_secure_dataset",
            "resource_kind": "secure_dataset",
            "project_id": project_id,
            "dataset_id": secure_dataset_id,
            "location": raw_location,
        }
    )

if view_query is not None and current_view_query(secure_view) != view_query:
    actions.append(
        {
            "kind": "upsert_secure_view",
            "resource_kind": "secure_view",
            "project_id": project_id,
            "dataset_id": secure_dataset_id,
            "view_id": secure_view_id,
            "view_query": view_query,
        }
    )

if not has_view_authorization(raw_dataset, project_id, secure_dataset_id, secure_view_id):
    actions.append(
        {
            "kind": "authorize_secure_view_on_raw_dataset",
            "resource_kind": "raw_dataset",
            "project_id": project_id,
            "dataset_id": raw_dataset_id,
            "secure_dataset_id": secure_dataset_id,
            "secure_view_id": secure_view_id,
        }
    )

if not has_viewer_binding(view_iam, maintenance_email):
    actions.append(
        {
            "kind": "grant_maintenance_viewer_on_secure_view",
            "resource_kind": "secure_view",
            "project_id": project_id,
            "dataset_id": secure_dataset_id,
            "view_id": secure_view_id,
            "member": f"serviceAccount:{maintenance_email}",
            "role": "roles/bigquery.dataViewer",
        }
    )

resource = {
    "project_id": project_id,
    "raw_dataset_id": raw_dataset_id,
    "raw_table_prefix": raw_table_prefix,
    "raw_table_id": raw_table_id,
    "raw_table_fqn": raw_table_fqn,
    "secure_dataset_id": secure_dataset_id,
    "secure_view_id": secure_view_id,
    "secure_view_fqn": f"{project_id}.{secure_dataset_id}.{secure_view_id}",
    "view_query": view_query,
}

discovery_material = json.dumps(
    {
        "project_iam": project_iam,
        "raw_dataset": raw_dataset,
        "raw_tables": raw_tables,
        "raw_table_iam": raw_table_iam,
        "secure_dataset": secure_dataset,
        "secure_view": secure_view,
        "view_iam": view_iam,
        "resource": resource,
    },
    sort_keys=True,
).encode("utf-8")

plan = {
    "generated_at_epoch": int(os.environ["PLAN_GENERATED_AT"]),
    "expires_at_epoch": int(os.environ["PLAN_EXPIRES_AT"]),
    "config_fingerprint": os.environ["PLAN_CONFIG_FINGERPRINT"],
    "operator_email": os.environ["PLAN_OPERATOR_EMAIL"],
    "discovery_hash": hashlib.sha256(discovery_material).hexdigest(),
    "status": "blocked" if blockers else "ready",
    "constraints": {
        "mutation_boundary": os.environ["PLAN_MUTATION_BOUNDARY"],
        "maintenance_job_user_role_required": True,
        "maintenance_raw_dataset_roles_forbidden": [
            "roles/bigquery.dataOwner",
            "roles/bigquery.dataEditor",
            "roles/bigquery.dataViewer",
        ],
    },
    "resource": resource,
    "actions": actions,
    "blockers": blockers,
}

Path(sys.argv[1]).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
PY

mim_task18_write_plan_json "$PLAN_TMP" "$PLAN_OUT"
printf 'Wrote reviewed plan to %s\n' "$PLAN_OUT"
