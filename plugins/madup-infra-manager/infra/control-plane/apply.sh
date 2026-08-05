#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=config_lib.sh
. "$SCRIPT_DIR/config_lib.sh"
# shellcheck source=iam/lib.sh
. "$SCRIPT_DIR/iam/lib.sh"

CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_default_config_file "$SCRIPT_DIR")}"
PROTECTED_PROJECTS_FILE="${MIM_PROTECTED_PROJECTS_FILE:-$(mim_default_protected_projects_file "$SCRIPT_DIR")}"
PLUGIN_ROOT=$(mim_plugin_root "$SCRIPT_DIR")
SNAPSHOT_HELPER=$(mim_snapshot_helper_path "$SCRIPT_DIR")

MODE=preview
PLAN_OUT=
PLAN_FILE=
TMP_DIR=
SNAPSHOT_DIR=
SNAPSHOT_CONFIG_FILE=
SNAPSHOT_PROTECTED_PROJECTS_FILE=
SERVICE_ACCOUNT_STATE_FILE=
SECRET_STATE_FILE=
BLOCKER_FILE=
ACTION_FILE=
WORKER_ARTIFACT_FILE=
DISCOVERY_JSON_FILE=
PLAN_JSON_FILE=

readonly MIM_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP=origin_request_claims
readonly MIM_FIRESTORE_REPLAY_CLAIM_TTL_FIELD_PATH=expires_at
readonly MIM_FIRESTORE_REPLAY_CLAIM_TTL_BLOCKER_CODE=firestore-replay-claim-ttl-invalid
readonly MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP=operations
readonly MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE=COLLECTION
readonly MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_BLOCKER_CODE=firestore-operations-dashboard-index-invalid

cleanup() {
  if [[ -n "${TMP_DIR:-}" && -d "$TMP_DIR" ]]; then
    rm -rf -- "$TMP_DIR"
  fi
  if [[ -n "${SNAPSHOT_DIR:-}" && -d "$SNAPSHOT_DIR" ]]; then
    rm -rf -- "$SNAPSHOT_DIR"
  fi
}
trap cleanup EXIT

usage() {
  printf 'Usage: %s [--plan --out .state/<name>.json | --apply --plan-file .state/<name>.json]\n' "$0" >&2
  exit 1
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --plan)
      [[ "$MODE" == preview ]] || usage
      MODE=plan
      shift
      ;;
    --out)
      [[ "$#" -ge 2 ]] || mim_fail "Missing value for --out"
      PLAN_OUT=$2
      shift 2
      ;;
    --apply)
      [[ "$MODE" == preview ]] || usage
      MODE=apply
      shift
      ;;
    --plan-file)
      [[ "$#" -ge 2 ]] || mim_fail "Missing value for --plan-file"
      PLAN_FILE=$2
      shift 2
      ;;
    --*)
      mim_fail "Unknown argument: $1"
      ;;
    *)
      mim_fail "Positional arguments are not supported"
      ;;
  esac
done

case "$MODE" in
  preview)
    [[ -z "$PLAN_OUT" && -z "$PLAN_FILE" ]] || usage
    ;;
  plan)
    [[ -n "$PLAN_OUT" && -z "$PLAN_FILE" ]] || usage
    ;;
  apply)
    [[ -n "$PLAN_FILE" && -z "$PLAN_OUT" ]] || usage
    ;;
esac

command -v gcloud >/dev/null 2>&1 || mim_fail "gcloud CLI is required"
command -v python3 >/dev/null 2>&1 || mim_fail "python3 is required"
command -v bq >/dev/null 2>&1 || mim_fail "bq CLI is required"
[[ -f "$SNAPSHOT_HELPER" ]] || mim_fail "Snapshot helper is required"

TMP_DIR=$(mktemp -d)
SERVICE_ACCOUNT_STATE_FILE="$TMP_DIR/service-accounts.tsv"
SECRET_STATE_FILE="$TMP_DIR/secrets.tsv"
BLOCKER_FILE="$TMP_DIR/blockers.tsv"
ACTION_FILE="$TMP_DIR/actions.tsv"
WORKER_ARTIFACT_FILE="$TMP_DIR/worker-artifacts.txt"
DISCOVERY_JSON_FILE="$TMP_DIR/discovery.json"
PLAN_JSON_FILE="$TMP_DIR/plan.json"

firestore_api_enabled_in_listing() {
  local enabled_apis=$1
  mim_has_exact_line "$enabled_apis" "firestore.googleapis.com"
}

firestore_replay_claim_ttl_missing_before_state_json() {
  python3 - <<'PY'
import json

print(
    json.dumps(
        {
            "collection_group": "origin_request_claims",
            "database": "(default)",
            "field_path": "expires_at",
            "status": "missing",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
PY
}

firestore_operations_dashboard_index_missing_before_state_json() {
  python3 - <<'PY'
import json

print(
    json.dumps(
        {
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
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
PY
}

snapshot_private_inputs() {
  SNAPSHOT_DIR=$(mktemp -d)
  chmod 700 "$SNAPSHOT_DIR"
  SNAPSHOT_CONFIG_FILE="$SNAPSHOT_DIR/config.env"
  SNAPSHOT_PROTECTED_PROJECTS_FILE="$SNAPSHOT_DIR/protected-projects.exact"

  python3 "$SNAPSHOT_HELPER" \
    --snapshot "Config file" "$CONFIG_FILE" "$SNAPSHOT_CONFIG_FILE" 65536 \
    --snapshot "Protected project file" "$PROTECTED_PROJECTS_FILE" "$SNAPSHOT_PROTECTED_PROJECTS_FILE" 1048576
}

gcloud_capture() {
  local description=$1
  shift
  local output
  if ! output=$(gcloud "$@" 2>/dev/null); then
    mim_fail "$description"
  fi
  printf '%s' "$output"
}

gcloud_optional_state() {
  local description=$1
  local output_file=$2
  shift 2

  local stderr_file
  local status
  stderr_file=$(mktemp)
  set +e
  gcloud "$@" >"$output_file" 2>"$stderr_file"
  status=$?
  set -e

  if [[ "$status" -eq 0 ]]; then
    rm -f -- "$stderr_file"
    printf 'exists'
    return
  fi

  if grep -Fq -- 'Cannot find service' "$stderr_file" || grep -Fq -- 'NOT_FOUND:' "$stderr_file"; then
    rm -f -- "$stderr_file"
    : >"$output_file"
    printf 'missing'
    return
  fi

  rm -f -- "$stderr_file"
  mim_fail "$description"
}

append_blocker() {
  local code=$1
  local detail=$2
  local message=$3
  printf '%s\t%s\t%s\n' "$code" "$detail" "$message" >>"$BLOCKER_FILE"
}

append_action() {
  local kind=$1
  local name=$2
  local value=${3:-}
  printf '%s\t%s\t%s\n' "$kind" "$name" "$value" >>"$ACTION_FILE"
}

append_firestore_replay_claim_ttl_action() {
  local before_state_json=$1
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "enable_firestore_replay_claim_ttl" \
    "$MIM_FIRESTORE_DATABASE" \
    "$MIM_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP" \
    "$MIM_FIRESTORE_REPLAY_CLAIM_TTL_FIELD_PATH" \
    "$before_state_json" >>"$ACTION_FILE"
}

append_firestore_operations_dashboard_index_action() {
  local before_state_json=$1
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "create_firestore_operations_dashboard_index" \
    "$MIM_FIRESTORE_DATABASE" \
    "$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP" \
    "$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE" \
    '[{"field_path":"workload_owner_id","order":"ASCENDING"},{"field_path":"workload_id","order":"ASCENDING"},{"field_path":"updated_at","order":"DESCENDING"},{"field_path":"created_at","order":"DESCENDING"},{"field_path":"id","order":"DESCENDING"}]' \
    "$before_state_json" >>"$ACTION_FILE"
}

collect_boundary() {
  mim_load_config "$SNAPSHOT_CONFIG_FILE"
  mim_assert_project_not_protected "$MIM_PROJECT_ID" "$SNAPSHOT_PROTECTED_PROJECTS_FILE"

  ACTIVE_ACCOUNT=$(gcloud_capture \
    "Unable to determine the active gcloud account" \
    auth list \
    --filter=status:ACTIVE \
    '--format=value(account)' \
    --account="$MIM_OPERATOR_EMAIL")
  [[ "$ACTIVE_ACCOUNT" == "$MIM_OPERATOR_EMAIL" ]] || mim_fail "Active gcloud account does not match the configured operator"

  PROJECT_ID_CHECK=$(gcloud_capture \
    "Unable to describe the configured project" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(projectId)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$PROJECT_ID_CHECK" == "$MIM_PROJECT_ID" ]] || mim_fail "Configured project mismatch"

  PROJECT_PARENT_TYPE=$(gcloud_capture \
    "Unable to determine the project parent type" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(parent.type)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$PROJECT_PARENT_TYPE" == "organization" ]] || mim_fail "Project parent must be an organization"

  PROJECT_PARENT_ID=$(gcloud_capture \
    "Unable to determine the project organization" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(parent.id)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$PROJECT_PARENT_ID" == "$MIM_ORGANIZATION_ID" ]] || mim_fail "Project organization mismatch"

  PROJECT_NUMBER=$(gcloud_capture \
    "Unable to determine the project number" \
    projects describe "$MIM_PROJECT_ID" \
    '--format=value(projectNumber)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || mim_fail "Invalid project number returned"

  BILLING_ENABLED=$(gcloud_capture \
    "Unable to determine project billing status" \
    billing projects describe "$MIM_PROJECT_ID" \
    '--format=value(billingEnabled)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$BILLING_ENABLED" == "True" ]] || mim_fail "Billing must be linked"

  BILLING_ACCOUNT_NAME=$(gcloud_capture \
    "Unable to determine the billing account" \
    billing projects describe "$MIM_PROJECT_ID" \
    '--format=value(billingAccountName)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ "$BILLING_ACCOUNT_NAME" == "billingAccounts/$MIM_BILLING_ACCOUNT_ID" ]] || mim_fail "Billing account mismatch"

  ENABLED_APIS_RAW=$(gcloud_capture \
    "Unable to list enabled APIs" \
    services list \
    --enabled \
    '--format=value(config.name)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")

  REQUIRED_APIS_EXISTING=
  REQUIRED_APIS_MISSING=
  while IFS= read -r api; do
    [[ -n "$api" ]] || continue
    if mim_has_exact_line "$ENABLED_APIS_RAW" "$api"; then
      REQUIRED_APIS_EXISTING+="${api}"$'\n'
    else
      REQUIRED_APIS_MISSING+="${api}"$'\n'
    fi
  done < <(mim_required_api_list)

  PROJECT_RUN_INVOKERS=$(gcloud_capture \
    "Unable to inspect project-wide Cloud Run invoker bindings" \
    projects get-iam-policy "$MIM_PROJECT_ID" \
    '--flatten=bindings[].members' \
    '--filter=bindings.role=roles/run.invoker' \
    '--format=value(bindings.members)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  [[ -z "$PROJECT_RUN_INVOKERS" ]] || mim_fail "Project-wide Cloud Run invoker bindings are forbidden"
}

discover_managed_identities() {
  : >"$SERVICE_ACCOUNT_STATE_FILE"
  while IFS=$'\t' read -r identity_role identity_name; do
    [[ -n "$identity_role" ]] || continue
    identity_email=$(mim_identity_email "$identity_name")
    identity_state_file="$TMP_DIR/identity-${identity_role}.txt"
    identity_state=$(gcloud_optional_state \
      "Unable to inspect managed identity $identity_name" \
      "$identity_state_file" \
      iam service-accounts describe "$identity_email" \
      '--format=value(email)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    if [[ "$identity_state" == "exists" ]]; then
      identity_email_value=$(<"$identity_state_file")
      [[ "$identity_email_value" == "$identity_email" ]] || mim_fail "Managed identity $identity_name must keep its dedicated email"
    fi
    printf '%s\t%s\t%s\n' "$identity_role" "$identity_name" "$identity_state" >>"$SERVICE_ACCOUNT_STATE_FILE"
  done < <(mim_managed_identity_rows)
}

discover_control_plane_service() {
  run_state_file="$TMP_DIR/control-plane-service.txt"
  RUN_SERVICE_STATE=$(gcloud_optional_state \
    "Unable to inspect the control-plane service" \
    "$run_state_file" \
    run services describe "$MIM_CONTROL_PLANE_SERVICE" \
    --region="$MIM_FIXED_REGION" \
    '--format=value(metadata.name)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  rm -f -- "$run_state_file"

  RUN_SERVICE_ACCOUNT=
  RUN_SERVICE_MIN=
  RUN_SERVICE_MAX=
  if [[ "$RUN_SERVICE_STATE" == "exists" ]]; then
    RUN_SERVICE_ACCOUNT=$(gcloud_capture \
      "Unable to inspect the control-plane runtime identity" \
      run services describe "$MIM_CONTROL_PLANE_SERVICE" \
      --region="$MIM_FIXED_REGION" \
      '--format=value(spec.template.spec.serviceAccountName)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$RUN_SERVICE_ACCOUNT" == "$(mim_identity_email mim-control-plane)" ]] || mim_fail "Cloud Run service must use the dedicated control-plane identity"

    RUN_SERVICE_MIN=$(gcloud_capture \
      "Unable to inspect the control-plane minimum instance setting" \
      run services describe "$MIM_CONTROL_PLANE_SERVICE" \
      --region="$MIM_FIXED_REGION" \
      '--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/minScale)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$RUN_SERVICE_MIN" == "0" ]] || mim_fail "Cloud Run service minimum instances must be 0"

    RUN_SERVICE_MAX=$(gcloud_capture \
      "Unable to inspect the control-plane maximum instance setting" \
      run services describe "$MIM_CONTROL_PLANE_SERVICE" \
      --region="$MIM_FIXED_REGION" \
      '--format=value(spec.template.metadata.annotations.autoscaling.knative.dev/maxScale)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$RUN_SERVICE_MAX" == "1" ]] || mim_fail "Cloud Run service maximum instances must be 1"
  fi
}

discover_artifact_repository() {
  artifact_state_file="$TMP_DIR/artifact-repository.txt"
  ARTIFACT_REPOSITORY_STATE=$(gcloud_optional_state \
    "Unable to inspect Artifact Registry repository" \
    "$artifact_state_file" \
    artifacts repositories describe "$MIM_ARTIFACT_REPOSITORY" \
    --location="$MIM_FIXED_REGION" \
    '--format=value(name)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  rm -f -- "$artifact_state_file"

  ARTIFACT_REPOSITORY_FORMAT=
  if [[ "$ARTIFACT_REPOSITORY_STATE" == "exists" ]]; then
    ARTIFACT_REPOSITORY_FORMAT=$(gcloud_capture \
      "Unable to inspect Artifact Registry repository format" \
      artifacts repositories describe "$MIM_ARTIFACT_REPOSITORY" \
      --location="$MIM_FIXED_REGION" \
      '--format=value(format)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$ARTIFACT_REPOSITORY_FORMAT" == "DOCKER" || "$ARTIFACT_REPOSITORY_FORMAT" == "docker" ]] || mim_fail "Artifact Registry repository must use docker format"
  fi
}

discover_firestore_database() {
  firestore_state_file="$TMP_DIR/firestore.txt"
  FIRESTORE_DATABASE_STATE=$(gcloud_optional_state \
    "Unable to inspect Firestore database" \
    "$firestore_state_file" \
    firestore databases describe "$MIM_FIRESTORE_DATABASE" \
    '--format=value(name)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  rm -f -- "$firestore_state_file"

  FIRESTORE_LOCATION=
  FIRESTORE_TYPE=
  if [[ "$FIRESTORE_DATABASE_STATE" == "exists" ]]; then
    FIRESTORE_LOCATION=$(gcloud_capture \
      "Unable to inspect Firestore location" \
      firestore databases describe "$MIM_FIRESTORE_DATABASE" \
      '--format=value(locationId)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$FIRESTORE_LOCATION" == "$MIM_FIXED_REGION" ]] || mim_fail "Firestore database location must stay in asia-northeast3"

    FIRESTORE_TYPE=$(gcloud_capture \
      "Unable to inspect Firestore database type" \
      firestore databases describe "$MIM_FIRESTORE_DATABASE" \
      '--format=value(type)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$FIRESTORE_TYPE" == "$MIM_FIRESTORE_TYPE" ]] || mim_fail "Firestore database must use Firestore Native mode"
  fi
}

discover_firestore_operations_dashboard_index() {
  FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS=missing
  FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATE=
  FIRESTORE_OPERATIONS_DASHBOARD_INDEX_RESOURCE_NAME=
  FIRESTORE_OPERATIONS_DASHBOARD_INDEX_DETAIL=
  FIRESTORE_OPERATIONS_DASHBOARD_INDEX_MESSAGE=

  if ! firestore_api_enabled_in_listing "$REQUIRED_APIS_EXISTING" || [[ "$FIRESTORE_DATABASE_STATE" != "exists" ]]; then
    return
  fi

  local index_state_file
  index_state_file="$TMP_DIR/firestore-indexes.json"
  if ! gcloud firestore indexes composite list \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" \
    --database="$MIM_FIRESTORE_DATABASE" >"$index_state_file" 2>/dev/null; then
    mim_fail "Unable to inspect Firestore composite indexes for operations"
  fi

  local index_contract_tsv
  index_contract_tsv=$(
    EXPECTED_PROJECT_ID="$MIM_PROJECT_ID" \
    EXPECTED_DATABASE="$MIM_FIRESTORE_DATABASE" \
    EXPECTED_COLLECTION_GROUP="$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP" \
    EXPECTED_QUERY_SCOPE="$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE" \
    python3 - "$index_state_file" <<'PY'
import json
import os
import re
import sys
from pathlib import Path


EXPECTED_FIELDS = [
    ("workload_owner_id", "ASCENDING"),
    ("workload_id", "ASCENDING"),
    ("updated_at", "DESCENDING"),
    ("created_at", "DESCENDING"),
    ("id", "DESCENDING"),
]
EXPECTED_FIELDS_WITH_NAME = EXPECTED_FIELDS + [("__name__", "DESCENDING")]


def emit(key: str, value: str) -> None:
    print(f"{key}\t{value}")


def invalid(detail: str, message: str, index_state: str = "", resource_name: str = "") -> None:
    emit("status", "blocked")
    emit("detail", detail)
    emit("message", message)
    if index_state:
        emit("index_state", index_state)
    if resource_name:
        emit("resource_name", resource_name)
    raise SystemExit(0)


def parse_fields(item, resource_name: str):
    fields = item.get("fields")
    if not isinstance(fields, list):
        invalid("fields-malformed", "Firestore operations dashboard index fields must be a JSON array", resource_name=resource_name)

    parsed = []
    for field in fields:
      if not isinstance(field, dict):
          invalid("field-entry-malformed", "Firestore operations dashboard index field entry must be an object", resource_name=resource_name)
      field_path = field.get("fieldPath")
      order = field.get("order")
      if not isinstance(field_path, str) or not isinstance(order, str):
          invalid("field-entry-malformed", "Firestore operations dashboard index field entry is missing fieldPath or order", resource_name=resource_name)
      parsed.append((field_path, order))
    return parsed


def is_matching_prefix(fields):
    if len(fields) < len(EXPECTED_FIELDS):
        return False
    return fields[: len(EXPECTED_FIELDS)] == EXPECTED_FIELDS


payload = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(payload, list):
    invalid("index-list-not-json-array", "Firestore operations dashboard index discovery must return a JSON array")

project_id = os.environ["EXPECTED_PROJECT_ID"]
database = os.environ["EXPECTED_DATABASE"]
collection_group = os.environ["EXPECTED_COLLECTION_GROUP"]
query_scope = os.environ["EXPECTED_QUERY_SCOPE"]
pattern = re.compile(r"^projects/([^/]+)/databases/([^/]+)/collectionGroups/([^/]+)/indexes/([^/]+)$")

equivalent = []
wrong_fields = []

for item in payload:
    if not isinstance(item, dict):
        invalid("index-entry-not-object", "Firestore operations dashboard index entry must be an object")
    resource_name = item.get("name")
    if not isinstance(resource_name, str):
        invalid("index-resource-missing", "Firestore operations dashboard index entry is missing its resource name")
    match = pattern.fullmatch(resource_name)
    if not match:
        invalid("index-resource-malformed", "Firestore operations dashboard index resource name is malformed", resource_name=resource_name)
    resource_project, resource_database, resource_collection_group, _ = match.groups()
    if resource_project != project_id or resource_database != database:
        continue
    if resource_collection_group != collection_group:
        continue

    item_query_scope = item.get("queryScope")
    if not isinstance(item_query_scope, str):
        invalid("query-scope-missing", "Firestore operations dashboard index query scope is missing", resource_name=resource_name)
    if item_query_scope != query_scope:
        continue

    index_state = item.get("state")
    if not isinstance(index_state, str):
        invalid("index-state-missing", "Firestore operations dashboard index state is missing", resource_name=resource_name)

    fields = parse_fields(item, resource_name)
    if fields == EXPECTED_FIELDS or fields == EXPECTED_FIELDS_WITH_NAME:
        equivalent.append((resource_name, index_state))
        continue
    wrong_fields.append((resource_name, index_state, is_matching_prefix(fields)))

if wrong_fields:
    resource_name, index_state, has_expected_prefix = wrong_fields[0]
    invalid(
        "ambiguous-extra-matching-index" if has_expected_prefix else "wrong-fields",
        (
            "Firestore operations dashboard index discovery found an ambiguous extra matching operations index"
            if has_expected_prefix
            else "Firestore operations dashboard index fields do not match the reviewed query contract"
        ),
        index_state=index_state,
        resource_name=resource_name,
    )

if len(equivalent) > 1:
    invalid(
        "duplicate-equivalent-index",
        "Firestore operations dashboard index discovery found duplicate equivalent operations indexes",
        index_state=equivalent[0][1],
        resource_name=equivalent[0][0],
    )

if not equivalent:
    emit("status", "missing")
    raise SystemExit(0)

resource_name, index_state = equivalent[0]
if index_state == "NEEDS_REPAIR":
    invalid(
        "needs-repair",
        "Firestore operations dashboard index requires repair",
        index_state=index_state,
        resource_name=resource_name,
    )
if index_state != "READY":
    invalid(
        "index-state-invalid",
        "Firestore operations dashboard index must be READY before the dashboard is released",
        index_state=index_state,
        resource_name=resource_name,
    )

emit("status", "configured")
emit("index_state", index_state)
emit("resource_name", resource_name)
PY
  )

  while IFS=$'\t' read -r key value; do
    case "$key" in
      status) FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS=$value ;;
      index_state) FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATE=$value ;;
      resource_name) FIRESTORE_OPERATIONS_DASHBOARD_INDEX_RESOURCE_NAME=$value ;;
      detail) FIRESTORE_OPERATIONS_DASHBOARD_INDEX_DETAIL=$value ;;
      message) FIRESTORE_OPERATIONS_DASHBOARD_INDEX_MESSAGE=$value ;;
    esac
  done <<<"$index_contract_tsv"

  if [[ "$FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS" == "blocked" ]]; then
    append_blocker \
      "$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_BLOCKER_CODE" \
      "${FIRESTORE_OPERATIONS_DASHBOARD_INDEX_DETAIL:-index-invalid}" \
      "${FIRESTORE_OPERATIONS_DASHBOARD_INDEX_MESSAGE:-Firestore operations dashboard index discovery drifted}"
  fi
}

discover_firestore_replay_claim_ttl() {
  FIRESTORE_REPLAY_CLAIM_TTL_STATUS=missing
  FIRESTORE_REPLAY_CLAIM_TTL_TTL_STATE=
  FIRESTORE_REPLAY_CLAIM_TTL_RESOURCE_NAME=
  FIRESTORE_REPLAY_CLAIM_TTL_EXPIRATION_OFFSET=
  FIRESTORE_REPLAY_CLAIM_TTL_DETAIL=
  FIRESTORE_REPLAY_CLAIM_TTL_MESSAGE=

  if ! firestore_api_enabled_in_listing "$REQUIRED_APIS_EXISTING" || [[ "$FIRESTORE_DATABASE_STATE" != "exists" ]]; then
    return
  fi

  local ttl_state_file
  ttl_state_file="$TMP_DIR/firestore-ttl.json"
  if ! gcloud firestore fields ttls list \
    --collection-group="$MIM_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP" \
    '--format=json' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" \
    --database="$MIM_FIRESTORE_DATABASE" >"$ttl_state_file" 2>/dev/null; then
    mim_fail "Unable to inspect Firestore TTL policy for origin_request_claims"
  fi

  local ttl_contract_tsv
  ttl_contract_tsv=$(
    EXPECTED_PROJECT_ID="$MIM_PROJECT_ID" \
    EXPECTED_DATABASE="$MIM_FIRESTORE_DATABASE" \
    EXPECTED_COLLECTION_GROUP="$MIM_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP" \
    EXPECTED_FIELD_PATH="$MIM_FIRESTORE_REPLAY_CLAIM_TTL_FIELD_PATH" \
    python3 - "$ttl_state_file" <<'PY'
import json
import os
import re
import sys
from pathlib import Path


def emit(key: str, value: str) -> None:
    print(f"{key}\t{value}")


def invalid(detail: str, message: str, ttl_state: str = "", resource_name: str = "", expiration_offset: str = "") -> None:
    emit("status", "blocked")
    emit("detail", detail)
    emit("message", message)
    if ttl_state:
      emit("ttl_state", ttl_state)
    if resource_name:
      emit("resource_name", resource_name)
    if expiration_offset:
      emit("expiration_offset", expiration_offset)
    raise SystemExit(0)


payload = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(payload, list):
    invalid("ttl-list-not-json-array", "Firestore replay-claim TTL discovery must return a JSON array")

project_id = os.environ["EXPECTED_PROJECT_ID"]
database = os.environ["EXPECTED_DATABASE"]
collection_group = os.environ["EXPECTED_COLLECTION_GROUP"]
field_path = os.environ["EXPECTED_FIELD_PATH"]
pattern = re.compile(r"^projects/([^/]+)/databases/([^/]+)/collectionGroups/([^/]+)/fields/([^/]+)$")

if not payload:
    emit("status", "missing")
    raise SystemExit(0)

parsed = []
for item in payload:
    if not isinstance(item, dict):
        invalid("ttl-entry-not-object", "Firestore replay-claim TTL entry must be an object")
    resource_name = item.get("name")
    if not isinstance(resource_name, str):
        invalid("ttl-resource-missing", "Firestore replay-claim TTL entry is missing its resource name")
    match = pattern.fullmatch(resource_name)
    if not match:
        invalid("ttl-resource-malformed", "Firestore replay-claim TTL resource name is malformed", resource_name=resource_name)
    resource_project, resource_database, resource_collection_group, resource_field_path = match.groups()
    if resource_project != project_id or resource_database != database or resource_collection_group != collection_group:
        invalid("ttl-resource-mismatch", "Firestore replay-claim TTL resource targets the wrong database or collection group", resource_name=resource_name)
    ttl_state = item.get("state")
    if not isinstance(ttl_state, str):
        invalid("ttl-state-missing", "Firestore replay-claim TTL state is missing", resource_name=resource_name)
    ttl_config = item.get("ttlConfig")
    if ttl_config is None:
        expiration_offset = ""
    elif isinstance(ttl_config, dict):
        expiration_offset = ttl_config.get("expirationOffset", "")
        expiration_offset = "" if expiration_offset is None else str(expiration_offset)
    else:
        invalid("ttl-config-malformed", "Firestore replay-claim TTL configuration is malformed", ttl_state=ttl_state, resource_name=resource_name)
    parsed.append(
        {
            "field_path": resource_field_path,
            "resource_name": resource_name,
            "state": ttl_state,
            "expiration_offset": expiration_offset,
        }
    )

if len(parsed) != 1:
    invalid("ttl-entry-count-invalid", "Firestore replay-claim TTL discovery must return at most one field for origin_request_claims")

entry = parsed[0]
if entry["field_path"] != field_path:
    invalid("ttl-field-mismatch", "Firestore replay-claim TTL targets the wrong field", ttl_state=entry["state"], resource_name=entry["resource_name"], expiration_offset=entry["expiration_offset"])
if entry["state"] not in {"ACTIVE", "CREATING"}:
    invalid("ttl-state-invalid", "Firestore replay-claim TTL state requires repair", ttl_state=entry["state"], resource_name=entry["resource_name"], expiration_offset=entry["expiration_offset"])
if entry["expiration_offset"] not in {"", "0", "0s", "0.0s"}:
    invalid("ttl-expiration-offset-invalid", "Firestore replay-claim TTL expiration offset must be absent or zero", ttl_state=entry["state"], resource_name=entry["resource_name"], expiration_offset=entry["expiration_offset"])

emit("status", "configured")
emit("ttl_state", entry["state"])
emit("resource_name", entry["resource_name"])
if entry["expiration_offset"]:
    emit("expiration_offset", entry["expiration_offset"])
PY
  )

  while IFS=$'\t' read -r key value; do
    case "$key" in
      status) FIRESTORE_REPLAY_CLAIM_TTL_STATUS=$value ;;
      ttl_state) FIRESTORE_REPLAY_CLAIM_TTL_TTL_STATE=$value ;;
      resource_name) FIRESTORE_REPLAY_CLAIM_TTL_RESOURCE_NAME=$value ;;
      expiration_offset) FIRESTORE_REPLAY_CLAIM_TTL_EXPIRATION_OFFSET=$value ;;
      detail) FIRESTORE_REPLAY_CLAIM_TTL_DETAIL=$value ;;
      message) FIRESTORE_REPLAY_CLAIM_TTL_MESSAGE=$value ;;
    esac
  done <<<"$ttl_contract_tsv"

  if [[ "$FIRESTORE_REPLAY_CLAIM_TTL_STATUS" == "blocked" ]]; then
    append_blocker \
      "$MIM_FIRESTORE_REPLAY_CLAIM_TTL_BLOCKER_CODE" \
      "${FIRESTORE_REPLAY_CLAIM_TTL_DETAIL:-ttl-invalid}" \
      "${FIRESTORE_REPLAY_CLAIM_TTL_MESSAGE:-Firestore replay-claim TTL discovery drifted}"
  fi
}

discover_private_queue() {
  queue_state_file="$TMP_DIR/private-queue.txt"
  PRIVATE_QUEUE_STATE=$(gcloud_optional_state \
    "Unable to inspect private worker queue" \
    "$queue_state_file" \
    tasks queues describe "$MIM_PRIVATE_QUEUE" \
    --location="$MIM_FIXED_REGION" \
    '--format=value(name)' \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID")
  rm -f -- "$queue_state_file"

  PRIVATE_QUEUE_RUNTIME_STATE=
  PRIVATE_QUEUE_MAX_ATTEMPTS=
  PRIVATE_QUEUE_MAX_RETRY_DURATION=
  PRIVATE_QUEUE_MIN_BACKOFF=
  PRIVATE_QUEUE_MAX_BACKOFF=
  PRIVATE_QUEUE_MAX_DOUBLINGS=
  if [[ "$PRIVATE_QUEUE_STATE" == "exists" ]]; then
    PRIVATE_QUEUE_RUNTIME_STATE=$(gcloud_capture \
      "Unable to inspect private worker queue state" \
      tasks queues describe "$MIM_PRIVATE_QUEUE" \
      --location="$MIM_FIXED_REGION" \
      '--format=value(state)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$PRIVATE_QUEUE_RUNTIME_STATE" == "$MIM_QUEUE_STATE" ]] || mim_fail "Private worker queue must remain RUNNING"

    PRIVATE_QUEUE_MAX_ATTEMPTS=$(gcloud_capture \
      "Unable to inspect private worker queue retry attempts" \
      tasks queues describe "$MIM_PRIVATE_QUEUE" \
      --location="$MIM_FIXED_REGION" \
      '--format=value(retryConfig.maxAttempts)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$PRIVATE_QUEUE_MAX_ATTEMPTS" == "$MIM_QUEUE_MAX_ATTEMPTS" ]] || mim_fail "Private worker queue max attempts must remain 4"

    PRIVATE_QUEUE_MAX_RETRY_DURATION=$(gcloud_capture \
      "Unable to inspect private worker queue retry duration" \
      tasks queues describe "$MIM_PRIVATE_QUEUE" \
      --location="$MIM_FIXED_REGION" \
      '--format=value(retryConfig.maxRetryDuration)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$PRIVATE_QUEUE_MAX_RETRY_DURATION" == "$MIM_QUEUE_MAX_RETRY_DURATION" ]] || mim_fail "Private worker queue max retry duration must remain 300s"

    PRIVATE_QUEUE_MIN_BACKOFF=$(gcloud_capture \
      "Unable to inspect private worker queue minimum backoff" \
      tasks queues describe "$MIM_PRIVATE_QUEUE" \
      --location="$MIM_FIXED_REGION" \
      '--format=value(retryConfig.minBackoff)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$PRIVATE_QUEUE_MIN_BACKOFF" == "$MIM_QUEUE_MIN_BACKOFF" ]] || mim_fail "Private worker queue minimum backoff must remain 5s"

    PRIVATE_QUEUE_MAX_BACKOFF=$(gcloud_capture \
      "Unable to inspect private worker queue maximum backoff" \
      tasks queues describe "$MIM_PRIVATE_QUEUE" \
      --location="$MIM_FIXED_REGION" \
      '--format=value(retryConfig.maxBackoff)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$PRIVATE_QUEUE_MAX_BACKOFF" == "$MIM_QUEUE_MAX_BACKOFF" ]] || mim_fail "Private worker queue maximum backoff must remain 60s"

    PRIVATE_QUEUE_MAX_DOUBLINGS=$(gcloud_capture \
      "Unable to inspect private worker queue backoff doublings" \
      tasks queues describe "$MIM_PRIVATE_QUEUE" \
      --location="$MIM_FIXED_REGION" \
      '--format=value(retryConfig.maxDoublings)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    [[ "$PRIVATE_QUEUE_MAX_DOUBLINGS" == "$MIM_QUEUE_MAX_DOUBLINGS" ]] || mim_fail "Private worker queue max doublings must remain 3"
  fi
}

discover_secrets() {
  : >"$SECRET_STATE_FILE"
  while IFS= read -r secret_name; do
    [[ -n "$secret_name" ]] || continue
    secret_state_file="$TMP_DIR/secret-${secret_name}.txt"
    secret_state=$(gcloud_optional_state \
      "Unable to inspect Secret Manager secret $secret_name" \
      "$secret_state_file" \
      secrets describe "$secret_name" \
      '--format=value(name)' \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID")
    rm -f -- "$secret_state_file"
    printf '%s\t%s\n' "$secret_name" "$secret_state" >>"$SECRET_STATE_FILE"
  done < <(mim_secret_names)
}

discover_local_blockers() {
  : >"$BLOCKER_FILE"
  : >"$WORKER_ARTIFACT_FILE"

  while IFS= read -r artifact_path; do
    [[ -n "$artifact_path" ]] || continue
    printf '%s\n' "$artifact_path" >>"$WORKER_ARTIFACT_FILE"
    if [[ ! -f "$PLUGIN_ROOT/$artifact_path" ]]; then
      append_blocker "missing-worker-artifact" "$artifact_path" "Required private worker artifact is missing"
    fi
  done <<'EOF'
control-plane/src/mim_control_plane/workers/deploy.py
control-plane/src/mim_control_plane/workers/identity_sync.py
control-plane/src/mim_control_plane/machine_api.py
control-plane/src/mim_control_plane/runtime.py
control-plane/src/mim_control_plane/workers/lifecycle.py
control-plane/src/mim_control_plane/workers/reconcile.py
control-plane/src/mim_control_plane/workers/usage_ingest.py
EOF

  while IFS= read -r artifact_path; do
    [[ -n "$artifact_path" ]] || continue
    printf '%s\n' "$artifact_path" >>"$WORKER_ARTIFACT_FILE"
    if [[ ! -f "$PLUGIN_ROOT/$artifact_path" ]]; then
      append_blocker "missing-reviewed-infra-artifact" "$artifact_path" "Reviewed infra plan/apply artifact is missing"
    fi
  done <<'EOF'
infra/release/plan.sh
infra/release/apply.sh
infra/edge/plan.sh
infra/edge/apply.sh
EOF
}

discover_central_iam_contract() {
  mim_capture_iam_contract "$SCRIPT_DIR" "$TMP_DIR" "$PROJECT_NUMBER"
  mim_iam_append_plan_findings "$BLOCKER_FILE" "$ACTION_FILE"
}

build_action_plan() {
  : >"$ACTION_FILE"

  while IFS= read -r api; do
    [[ -n "$api" ]] || continue
    append_action "enable_api" "$api"
  done <<<"$REQUIRED_APIS_MISSING"

  while IFS=$'\t' read -r identity_role identity_name identity_state; do
    [[ -n "$identity_role" ]] || continue
    if [[ "$identity_state" == "missing" ]]; then
      append_action "create_service_account" "$identity_role" "$identity_name"
    fi
  done <"$SERVICE_ACCOUNT_STATE_FILE"

  [[ "$ARTIFACT_REPOSITORY_STATE" == "missing" ]] && append_action "create_artifact_repository" "$MIM_ARTIFACT_REPOSITORY"
  [[ "$FIRESTORE_DATABASE_STATE" == "missing" ]] && append_action "create_firestore_database" "$MIM_FIRESTORE_DATABASE"
  if [[ "$FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS" == "missing" ]]; then
    append_firestore_operations_dashboard_index_action "$(firestore_operations_dashboard_index_missing_before_state_json)"
  fi
  if [[ "$FIRESTORE_REPLAY_CLAIM_TTL_STATUS" == "missing" ]]; then
    append_firestore_replay_claim_ttl_action "$(firestore_replay_claim_ttl_missing_before_state_json)"
  fi
  [[ "$PRIVATE_QUEUE_STATE" == "missing" ]] && append_action "create_tasks_queue" "$MIM_PRIVATE_QUEUE"

  while IFS=$'\t' read -r secret_name secret_state; do
    [[ -n "$secret_name" ]] || continue
    [[ "$secret_state" == "missing" ]] && append_action "create_secret" "$secret_name"
  done <"$SECRET_STATE_FILE"
}

write_plan_and_discovery() {
  local generated_at=$1
  local expires_at=$2
  local config_fingerprint
  local protected_projects_fingerprint
  config_fingerprint=$(mim_sha256_file "$SNAPSHOT_CONFIG_FILE")
  protected_projects_fingerprint=$(mim_sha256_file "$SNAPSHOT_PROTECTED_PROJECTS_FILE")

  PLAN_GENERATED_AT="$generated_at" \
  PLAN_EXPIRES_AT="$expires_at" \
  PLAN_CONFIG_FINGERPRINT="$config_fingerprint" \
  PLAN_PROTECTED_PROJECTS_FINGERPRINT="$protected_projects_fingerprint" \
  PLAN_VERSION="$MIM_PLAN_VERSION" \
  PLAN_FIXED_REGION="$MIM_FIXED_REGION" \
  PLAN_SERVICE_NAME="$MIM_CONTROL_PLANE_SERVICE" \
  PLAN_ARTIFACT_REPOSITORY="$MIM_ARTIFACT_REPOSITORY" \
  PLAN_PRIVATE_QUEUE="$MIM_PRIVATE_QUEUE" \
  PLAN_FIRESTORE_DATABASE="$MIM_FIRESTORE_DATABASE" \
  PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP="$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP" \
  PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE="$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP="$MIM_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_FIELD_PATH="$MIM_FIRESTORE_REPLAY_CLAIM_TTL_FIELD_PATH" \
  PLAN_OPERATOR_EMAIL="$MIM_OPERATOR_EMAIL" \
  PLAN_PROJECT_ID="$MIM_PROJECT_ID" \
  PLAN_ORGANIZATION_ID="$MIM_ORGANIZATION_ID" \
  PLAN_BILLING_ACCOUNT_ID="$MIM_BILLING_ACCOUNT_ID" \
  PLAN_CLOUDFLARE_ACCOUNT_ID="$MIM_CLOUDFLARE_ACCOUNT_ID" \
  PLAN_CLOUDFLARE_ZONE_ID="$MIM_CLOUDFLARE_ZONE_ID" \
  PLAN_CLOUDFLARE_TEAM_NAME="$MIM_CLOUDFLARE_TEAM_NAME" \
  PLAN_GITHUB_REPOSITORY_IDS="$MIM_GITHUB_REPOSITORY_IDS" \
  PLAN_SLACK_APP_ID="$MIM_SLACK_APP_ID" \
  PLAN_SLACK_APPROVED_ORG_ID="$MIM_SLACK_APPROVED_ORG_ID" \
  PLAN_SLACK_APPROVED_WORKSPACE_IDS="$MIM_SLACK_APPROVED_WORKSPACE_IDS" \
  PLAN_REQUIRED_APIS_EXISTING="$REQUIRED_APIS_EXISTING" \
  PLAN_REQUIRED_APIS_MISSING="$REQUIRED_APIS_MISSING" \
  PLAN_RUN_SERVICE_STATE="$RUN_SERVICE_STATE" \
  PLAN_RUN_SERVICE_ACCOUNT="${RUN_SERVICE_ACCOUNT:-}" \
  PLAN_RUN_SERVICE_MIN="${RUN_SERVICE_MIN:-}" \
  PLAN_RUN_SERVICE_MAX="${RUN_SERVICE_MAX:-}" \
  PLAN_ARTIFACT_REPOSITORY_STATE="$ARTIFACT_REPOSITORY_STATE" \
  PLAN_ARTIFACT_REPOSITORY_FORMAT="${ARTIFACT_REPOSITORY_FORMAT:-}" \
  PLAN_FIRESTORE_DATABASE_STATE="$FIRESTORE_DATABASE_STATE" \
  PLAN_FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-}" \
  PLAN_FIRESTORE_TYPE="${FIRESTORE_TYPE:-}" \
  PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS="$FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS" \
  PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATE="${FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATE:-}" \
  PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_RESOURCE_NAME="${FIRESTORE_OPERATIONS_DASHBOARD_INDEX_RESOURCE_NAME:-}" \
  PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_DETAIL="${FIRESTORE_OPERATIONS_DASHBOARD_INDEX_DETAIL:-}" \
  PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_MESSAGE="${FIRESTORE_OPERATIONS_DASHBOARD_INDEX_MESSAGE:-}" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_STATUS="$FIRESTORE_REPLAY_CLAIM_TTL_STATUS" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_TTL_STATE="${FIRESTORE_REPLAY_CLAIM_TTL_TTL_STATE:-}" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_RESOURCE_NAME="${FIRESTORE_REPLAY_CLAIM_TTL_RESOURCE_NAME:-}" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_EXPIRATION_OFFSET="${FIRESTORE_REPLAY_CLAIM_TTL_EXPIRATION_OFFSET:-}" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_DETAIL="${FIRESTORE_REPLAY_CLAIM_TTL_DETAIL:-}" \
  PLAN_FIRESTORE_REPLAY_CLAIM_TTL_MESSAGE="${FIRESTORE_REPLAY_CLAIM_TTL_MESSAGE:-}" \
  PLAN_PRIVATE_QUEUE_STATE="$PRIVATE_QUEUE_STATE" \
  PLAN_PRIVATE_QUEUE_RUNTIME_STATE="${PRIVATE_QUEUE_RUNTIME_STATE:-}" \
  PLAN_SERVICE_ACCOUNT_STATE_FILE="$SERVICE_ACCOUNT_STATE_FILE" \
  PLAN_SECRET_STATE_FILE="$SECRET_STATE_FILE" \
  PLAN_BLOCKER_FILE="$BLOCKER_FILE" \
  PLAN_ACTION_FILE="$ACTION_FILE" \
  PLAN_WORKER_ARTIFACT_FILE="$WORKER_ARTIFACT_FILE" \
  PLAN_DISCOVERY_JSON_FILE="$DISCOVERY_JSON_FILE" \
  PLAN_PLAN_JSON_FILE="$PLAN_JSON_FILE" \
  PLAN_IAM_EVALUATION_FILE="$MIM_IAM_EVALUATION_FILE" \
  python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path


def read_tsv(path: str):
    rows = []
    for raw in Path(path).read_text().splitlines():
        if not raw:
            continue
        rows.append(raw.split("\t"))
    return rows


service_accounts = {}
for role, service_account_name, status in read_tsv(os.environ["PLAN_SERVICE_ACCOUNT_STATE_FILE"]):
    service_accounts[role] = {
        "email": f"{service_account_name}@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "service_account_name": service_account_name,
        "status": status,
    }

secrets = {}
for secret_name, status in read_tsv(os.environ["PLAN_SECRET_STATE_FILE"]):
    secrets[secret_name] = {"status": status}

blockers = []
for code, detail, message in read_tsv(os.environ["PLAN_BLOCKER_FILE"]):
    blockers.append({"code": code, "detail": detail, "message": message})

actions = []
for row in read_tsv(os.environ["PLAN_ACTION_FILE"]):
    kind = row[0]
    if kind == "create_firestore_operations_dashboard_index":
        fields = json.loads(row[4])
        before_state = json.loads(row[5])
        item = {
            "kind": kind,
            "database": row[1],
            "collection_group": row[2],
            "query_scope": row[3],
            "fields": fields,
            "before_state": before_state,
            "before_state_hash": hashlib.sha256(
                json.dumps(before_state, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    elif kind == "enable_firestore_replay_claim_ttl":
        before_state = json.loads(row[4])
        item = {
            "kind": kind,
            "database": row[1],
            "collection_group": row[2],
            "field_path": row[3],
            "before_state": before_state,
            "before_state_hash": hashlib.sha256(
                json.dumps(before_state, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    else:
        name = row[1]
        item = {"kind": kind, "name": name}
        if len(row) > 2 and row[2] and row[2] != "__empty__":
            item["value"] = row[2]
        if len(row) > 3 and row[3] and row[3] != "__empty__":
            item["member"] = row[3]
        if len(row) > 4 and row[4] and row[4] != "__empty__":
            item["condition_title"] = row[4]
        if len(row) > 5 and row[5] and row[5] != "__empty__":
            item["condition_expression"] = row[5]
    actions.append(item)

worker_artifacts = [line for line in Path(os.environ["PLAN_WORKER_ARTIFACT_FILE"]).read_text().splitlines() if line]
required_apis_existing = [line for line in os.environ["PLAN_REQUIRED_APIS_EXISTING"].splitlines() if line]
required_apis_missing = [line for line in os.environ["PLAN_REQUIRED_APIS_MISSING"].splitlines() if line]
status = "blocked" if blockers else "ready"
iam_evaluation = json.loads(Path(os.environ["PLAN_IAM_EVALUATION_FILE"]).read_text())

firestore_operations_dashboard_index_state = {
    "status": os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS"],
    "database": os.environ["PLAN_FIRESTORE_DATABASE"],
    "collection_group": os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP"],
    "query_scope": os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE"],
    "fields": [
        {"field_path": "workload_owner_id", "order": "ASCENDING"},
        {"field_path": "workload_id", "order": "ASCENDING"},
        {"field_path": "updated_at", "order": "DESCENDING"},
        {"field_path": "created_at", "order": "DESCENDING"},
        {"field_path": "id", "order": "DESCENDING"},
    ],
}
if os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATE"]:
    firestore_operations_dashboard_index_state["index_state"] = os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATE"]
if os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_RESOURCE_NAME"]:
    firestore_operations_dashboard_index_state["resource_name"] = os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_RESOURCE_NAME"]
if os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_DETAIL"]:
    firestore_operations_dashboard_index_state["detail"] = os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_DETAIL"]
if os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_MESSAGE"]:
    firestore_operations_dashboard_index_state["message"] = os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_MESSAGE"]

firestore_replay_claim_ttl_state = {
    "status": os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_STATUS"],
    "database": os.environ["PLAN_FIRESTORE_DATABASE"],
    "collection_group": os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP"],
    "field_path": os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_FIELD_PATH"],
}
if os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_TTL_STATE"]:
    firestore_replay_claim_ttl_state["ttl_state"] = os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_TTL_STATE"]
if os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_RESOURCE_NAME"]:
    firestore_replay_claim_ttl_state["resource_name"] = os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_RESOURCE_NAME"]
if os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_EXPIRATION_OFFSET"]:
    firestore_replay_claim_ttl_state["expiration_offset"] = os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_EXPIRATION_OFFSET"]
if os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_DETAIL"]:
    firestore_replay_claim_ttl_state["detail"] = os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_DETAIL"]
if os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_MESSAGE"]:
    firestore_replay_claim_ttl_state["message"] = os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_MESSAGE"]

discovery = {
    "enabled_apis": {
        "existing": required_apis_existing,
        "missing": required_apis_missing,
    },
    "service_accounts": service_accounts,
    "artifact_repository": {
        "status": os.environ["PLAN_ARTIFACT_REPOSITORY_STATE"],
        "location": os.environ["PLAN_FIXED_REGION"],
        "format": os.environ["PLAN_ARTIFACT_REPOSITORY_FORMAT"] or "DOCKER",
    },
    "firestore_database": {
        "status": os.environ["PLAN_FIRESTORE_DATABASE_STATE"],
        "location": os.environ["PLAN_FIRESTORE_LOCATION"] or os.environ["PLAN_FIXED_REGION"],
        "type": os.environ["PLAN_FIRESTORE_TYPE"] or "FIRESTORE_NATIVE",
    },
    "firestore_operations_dashboard_index": firestore_operations_dashboard_index_state,
    "firestore_replay_claim_ttl": firestore_replay_claim_ttl_state,
    "tasks_queue": {
        "status": os.environ["PLAN_PRIVATE_QUEUE_STATE"],
        "location": os.environ["PLAN_FIXED_REGION"],
        "state": os.environ["PLAN_PRIVATE_QUEUE_RUNTIME_STATE"] or "RUNNING",
        "oidc_service_account": f"mim-deploy-worker@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "retry": {
            "max_attempts": 4,
            "max_retry_duration": "300s",
            "min_backoff": "5s",
            "max_backoff": "60s",
            "max_doublings": 3,
        },
    },
    "secrets": secrets,
    "control_plane_service": {
        "status": os.environ["PLAN_RUN_SERVICE_STATE"],
        "service_account": os.environ["PLAN_RUN_SERVICE_ACCOUNT"] or None,
        "min_instances": int(os.environ["PLAN_RUN_SERVICE_MIN"]) if os.environ["PLAN_RUN_SERVICE_MIN"] else None,
        "max_instances": int(os.environ["PLAN_RUN_SERVICE_MAX"]) if os.environ["PLAN_RUN_SERVICE_MAX"] else None,
    },
    "iam": iam_evaluation["observed"],
}

Path(os.environ["PLAN_DISCOVERY_JSON_FILE"]).write_text(json.dumps(discovery, indent=2, sort_keys=True) + "\n")

plan = {
    "version": os.environ["PLAN_VERSION"],
    "generated_at_epoch": int(os.environ["PLAN_GENERATED_AT"]),
    "iam_contract": iam_evaluation["contract"],
    "expires_at_epoch": int(os.environ["PLAN_EXPIRES_AT"]),
    "status": status,
    "blockers": blockers,
    "config": {
        "operator_email": os.environ["PLAN_OPERATOR_EMAIL"],
        "project_id": os.environ["PLAN_PROJECT_ID"],
        "organization_id": os.environ["PLAN_ORGANIZATION_ID"],
        "billing_account_id": os.environ["PLAN_BILLING_ACCOUNT_ID"],
        "config_fingerprint": os.environ["PLAN_CONFIG_FINGERPRINT"],
        "protected_projects_fingerprint": os.environ["PLAN_PROTECTED_PROJECTS_FINGERPRINT"],
    },
    "targets": {
        "region": os.environ["PLAN_FIXED_REGION"],
        "service_name": os.environ["PLAN_SERVICE_NAME"],
        "artifact_repository": os.environ["PLAN_ARTIFACT_REPOSITORY"],
        "tasks_queue": os.environ["PLAN_PRIVATE_QUEUE"],
        "firestore_database": os.environ["PLAN_FIRESTORE_DATABASE"],
        "firestore_operations_dashboard_index": {
            "collection_group": os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP"],
            "database": os.environ["PLAN_FIRESTORE_DATABASE"],
            "fields": [
                {"field_path": "workload_owner_id", "order": "ASCENDING"},
                {"field_path": "workload_id", "order": "ASCENDING"},
                {"field_path": "updated_at", "order": "DESCENDING"},
                {"field_path": "created_at", "order": "DESCENDING"},
                {"field_path": "id", "order": "DESCENDING"},
            ],
            "query_scope": os.environ["PLAN_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE"],
        },
        "firestore_replay_claim_ttl": {
            "collection_group": os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_COLLECTION_GROUP"],
            "database": os.environ["PLAN_FIRESTORE_DATABASE"],
            "field_path": os.environ["PLAN_FIRESTORE_REPLAY_CLAIM_TTL_FIELD_PATH"],
        },
    },
    "managed_service_accounts": {
        "control_plane": f"mim-control-plane@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "deploy_worker": f"mim-deploy-worker@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "build": f"mim-build@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "schedule_gateway": f"mim-schedule-gateway@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "maintenance": f"mim-maintenance@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "identity_sync": f"mim-identity-sync@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "release": f"mim-release@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
    },
    "required_apis": required_apis_existing + required_apis_missing,
    "required_secrets": sorted(secrets.keys()),
    "constraints": {
        "min_instances": 0,
        "max_instances": 1,
        "project_wide_invoker_forbidden": True,
        "runtime_project_roles_forbidden": True,
        "direct_origin_denied_by_hmac": True,
        "cloudflare_transport_required": True,
        "transport_mutations_disabled": True,
        "service_mutations_disabled": True,
        "protected_project_boundary_enforced": True,
    },
    "private_worker_expectations": {
        "queue_name": os.environ["PLAN_PRIVATE_QUEUE"],
        "oidc_service_account": f"mim-deploy-worker@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com",
        "queue_retry": {
            "max_attempts": 4,
            "max_retry_duration": "300s",
            "min_backoff": "5s",
            "max_backoff": "60s",
            "max_doublings": 3,
        },
        "required_worker_artifacts": worker_artifacts,
    },
    "initial_state": discovery,
    "actions": actions,
    "discovery_hash": "",
}

discovery_hash = __import__("hashlib").sha256(
    json.dumps(discovery, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
plan["discovery_hash"] = discovery_hash

Path(os.environ["PLAN_PLAN_JSON_FILE"]).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
PY
}

collect_current_contract() {
  snapshot_private_inputs
  collect_boundary
  discover_managed_identities
  discover_control_plane_service
  discover_artifact_repository
  discover_firestore_database
  # Initialize the blocker inventory before any remote discovery can append to
  # it.  discover_local_blockers owns the truncation; running it later would
  # silently erase Firestore TTL drift found below.
  discover_local_blockers
  discover_firestore_operations_dashboard_index
  discover_firestore_replay_claim_ttl
  discover_private_queue
  discover_secrets
  build_action_plan
  discover_central_iam_contract
}

write_reviewed_plan() {
  local output_path=$1
  local generated_at expires_at

  generated_at=$(mim_now_epoch)
  expires_at=$((generated_at + MIM_PLAN_MAX_AGE_SECONDS))

  write_plan_and_discovery "$generated_at" "$expires_at"
  cp "$PLAN_JSON_FILE" "$output_path"
  chmod 600 "$output_path"
  printf '%s  %s\n' "$(mim_sha256_file "$output_path")" "$(basename "$output_path")" >"$output_path.sha256"
  chmod 600 "$output_path.sha256"
}

validate_reviewed_plan() {
  local plan_path=$1
  local now_epoch expected_hash actual_hash generated_at expires_at current_discovery_hash plan_discovery_hash current_protected_projects_fingerprint plan_protected_projects_fingerprint

  expected_hash=$(awk '{print $1}' "$plan_path.sha256")
  actual_hash=$(mim_sha256_file "$plan_path")
  [[ "$expected_hash" == "$actual_hash" ]] || mim_fail "Plan hash verification failed"

  now_epoch=$(mim_now_epoch)
  generated_at=$(python3 - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["generated_at_epoch"])
PY
)
  expires_at=$(python3 - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["expires_at_epoch"])
PY
)
  [[ "$generated_at" =~ ^[0-9]+$ ]] || mim_fail "Plan file does not match the expected reviewed contract"
  [[ "$expires_at" =~ ^[0-9]+$ ]] || mim_fail "Plan file does not match the expected reviewed contract"
  (( generated_at <= now_epoch )) || mim_fail "Plan generated_at cannot be in the future"
  (( expires_at - generated_at == MIM_PLAN_MAX_AGE_SECONDS )) || mim_fail "Plan expiry must be exactly 1800 seconds after generation"
  (( now_epoch <= expires_at )) || mim_fail "Plan is older than 30 minutes"

  if ! python3 - "$plan_path" <<'PY'
import json
import re
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
expected_top = {
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
expected_config = {
    "billing_account_id",
    "config_fingerprint",
    "operator_email",
    "organization_id",
    "project_id",
    "protected_projects_fingerprint",
}
if set(data.keys()) != expected_top:
    raise SystemExit(1)
if set(data["config"].keys()) != expected_config:
    raise SystemExit(1)
if data["version"] != "mim-control-plane-plan-v2":
    raise SystemExit(1)
if data["status"] not in {"ready", "blocked"}:
    raise SystemExit(1)
if not isinstance(data["generated_at_epoch"], int):
    raise SystemExit(1)
if not isinstance(data["expires_at_epoch"], int):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{64}", data["config"]["config_fingerprint"]):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{64}", data["config"]["protected_projects_fingerprint"]):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{64}", data["discovery_hash"]):
    raise SystemExit(1)
PY
  then
    mim_fail "Plan file does not match the expected reviewed contract"
  fi

  write_plan_and_discovery "$generated_at" "$expires_at"
  current_protected_projects_fingerprint=$(mim_sha256_file "$SNAPSHOT_PROTECTED_PROJECTS_FILE")
  plan_protected_projects_fingerprint=$(python3 - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["config"]["protected_projects_fingerprint"])
PY
)
  [[ "$plan_protected_projects_fingerprint" == "$current_protected_projects_fingerprint" ]] || mim_fail "Protected project fingerprint mismatch"

  current_discovery_hash=$(mim_sha256_file "$DISCOVERY_JSON_FILE")
  plan_discovery_hash=$(python3 - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["discovery_hash"])
PY
)

  if [[ "$plan_discovery_hash" != "$(python3 - "$PLAN_JSON_FILE" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["discovery_hash"])
PY
)" ]]; then
    mim_fail "Discovery drift detected"
  fi

  if ! python3 - "$plan_path" "$PLAN_JSON_FILE" <<'PY'
import json
import sys
from pathlib import Path

actual = json.loads(Path(sys.argv[1]).read_text())
expected = json.loads(Path(sys.argv[2]).read_text())
if actual != expected:
    raise SystemExit(1)
PY
  then
    mim_fail "Plan file does not match the expected reviewed contract"
  fi
  : "$current_discovery_hash"
}

apply_reviewed_actions() {
  local plan_path=$1
  local status
  status=$(python3 - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["status"])
PY
)

  [[ "$status" == "ready" ]] || mim_fail "Reviewed plan contains blockers"

  python3 - "$plan_path" <<'PY' >"$TMP_DIR/apply-actions.tsv"
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
for action in data["actions"]:
    if action["kind"] == "create_firestore_operations_dashboard_index":
        print(
            "\t".join(
                [
                    action["kind"],
                    action["database"],
                    action["collection_group"],
                    action["query_scope"],
                    json.dumps(action["fields"], sort_keys=True, separators=(",", ":")),
                    json.dumps(action["before_state"], sort_keys=True, separators=(",", ":")),
                    action["before_state_hash"],
                ]
            )
        )
        continue
    if action["kind"] == "enable_firestore_replay_claim_ttl":
        print(
            "\t".join(
                [
                    action["kind"],
                    action["database"],
                    action["collection_group"],
                    action["field_path"],
                    json.dumps(action["before_state"], sort_keys=True, separators=(",", ":")),
                    action["before_state_hash"],
                ]
            )
        )
        continue
    print(
        "\t".join(
            [
                action["kind"],
                action["name"],
                action.get("value", "__empty__"),
                action.get("member", "__empty__"),
                action.get("condition_title", "__empty__"),
                action.get("condition_expression", "__empty__"),
            ]
        )
    )
PY

  while IFS=$'\t' read -r kind name value member condition_title condition_expression extra; do
    [[ -n "$kind" ]] || continue
    [[ "$value" == "__empty__" ]] && value=
    [[ "$member" == "__empty__" ]] && member=
    [[ "$condition_title" == "__empty__" ]] && condition_title=
    [[ "$condition_expression" == "__empty__" ]] && condition_expression=
    [[ "${extra:-}" == "__empty__" ]] && extra=
    case "$kind" in
      enable_api)
        gcloud services enable "$name" \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      create_service_account)
        gcloud iam service-accounts create "$value" \
          '--display-name=MIM managed identity' \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      create_artifact_repository)
        gcloud artifacts repositories create "$name" \
          --location="$MIM_FIXED_REGION" \
          --repository-format=docker \
          --description='MIM control-plane images' \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      create_firestore_database)
        gcloud firestore databases create "$name" \
          --location="$MIM_FIXED_REGION" \
          --type=firestore-native \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      create_firestore_operations_dashboard_index)
        if [[ "$(python3 - "$condition_expression" <<'PY'
import hashlib
import json
import sys

before_state = json.loads(sys.argv[1])
actual = hashlib.sha256(json.dumps(before_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
print(actual)
PY
)" != "$extra" ]]; then
          mim_fail "Plan file does not match the expected reviewed contract"
        fi
        if [[ "$condition_title" != '[{"field_path":"workload_owner_id","order":"ASCENDING"},{"field_path":"workload_id","order":"ASCENDING"},{"field_path":"updated_at","order":"DESCENDING"},{"field_path":"created_at","order":"DESCENDING"},{"field_path":"id","order":"DESCENDING"}]' ]]; then
          mim_fail "Plan file does not match the expected reviewed contract"
        fi
        if [[ "$value" != "$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_COLLECTION_GROUP" || "$member" != "$MIM_FIRESTORE_OPERATIONS_DASHBOARD_INDEX_QUERY_SCOPE" ]]; then
          mim_fail "Plan file does not match the expected reviewed contract"
        fi
        gcloud firestore indexes composite create \
          --field-config=field-path=workload_owner_id,order=ascending \
          --field-config=field-path=workload_id,order=ascending \
          --field-config=field-path=updated_at,order=descending \
          --field-config=field-path=created_at,order=descending \
          --field-config=field-path=id,order=descending \
          --database="$name" \
          --collection-group="$value" \
          --query-scope=collection \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        REQUIRED_APIS_EXISTING="${REQUIRED_APIS_EXISTING}"$'\nfirestore.googleapis.com\n'
        FIRESTORE_DATABASE_STATE=exists
        discover_firestore_operations_dashboard_index
        if [[ "$FIRESTORE_OPERATIONS_DASHBOARD_INDEX_STATUS" != "configured" ]]; then
          mim_fail "Readback verification failed"
        fi
        ;;
      enable_firestore_replay_claim_ttl)
        if [[ "$(python3 - "$condition_title" "$condition_expression" <<'PY'
import hashlib
import json
import sys

before_state = json.loads(sys.argv[1])
actual = hashlib.sha256(json.dumps(before_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
print(actual)
PY
)" != "$condition_expression" ]]; then
          mim_fail "Plan file does not match the expected reviewed contract"
        fi
        gcloud firestore fields ttls update "$member" \
          --collection-group="$value" \
          --database="$name" \
          --enable-ttl \
          --async \
          --quiet \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        # The API, database, and composite-index actions are ordered before
        # this action. Force an exact TTL readback instead of reusing the
        # pre-apply discovery snapshot; an async CREATING state is accepted as
        # converged.
        REQUIRED_APIS_EXISTING="${REQUIRED_APIS_EXISTING}"$'\nfirestore.googleapis.com\n'
        FIRESTORE_DATABASE_STATE=exists
        discover_firestore_replay_claim_ttl
        if [[ "$FIRESTORE_REPLAY_CLAIM_TTL_STATUS" != "configured" ]]; then
          mim_fail "Readback verification failed"
        fi
        ;;
      create_tasks_queue)
        gcloud tasks queues create "$name" \
          --location="$MIM_FIXED_REGION" \
          --max-attempts="$MIM_QUEUE_MAX_ATTEMPTS" \
          --max-retry-duration="$MIM_QUEUE_MAX_RETRY_DURATION" \
          --min-backoff="$MIM_QUEUE_MIN_BACKOFF" \
          --max-backoff="$MIM_QUEUE_MAX_BACKOFF" \
          --max-doublings="$MIM_QUEUE_MAX_DOUBLINGS" \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      create_secret)
        gcloud secrets create "$name" \
          --replication-policy=automatic \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      bind_project_role)
        if [[ -n "$condition_title" && -n "$condition_expression" ]]; then
          gcloud projects add-iam-policy-binding "$MIM_PROJECT_ID" \
            --member="$member" \
            --role="$name" \
            "--condition=expression=$condition_expression,title=$condition_title" \
            --account="$MIM_OPERATOR_EMAIL" \
            --project="$MIM_PROJECT_ID" >/dev/null
        else
          gcloud projects add-iam-policy-binding "$MIM_PROJECT_ID" \
            --member="$member" \
            --role="$name" \
            --account="$MIM_OPERATOR_EMAIL" \
            --project="$MIM_PROJECT_ID" >/dev/null
        fi
        ;;
      bind_service_account_role)
        gcloud iam service-accounts add-iam-policy-binding "$value" \
          --member="$member" \
          --role="$name" \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      bind_artifact_repository_role)
        gcloud artifacts repositories add-iam-policy-binding "$value" \
          --location="$MIM_FIXED_REGION" \
          --member="$member" \
          --role="$name" \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      bind_secret_resource_role)
        gcloud secrets add-iam-policy-binding "${value##*/}" \
          --member="$member" \
          --role="$name" \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        ;;
      *)
        mim_fail "Plan file does not match the expected reviewed contract"
        ;;
    esac
  done <"$TMP_DIR/apply-actions.tsv"
}

collect_current_contract

case "$MODE" in
  preview)
    write_reviewed_plan "$TMP_DIR/preview.json"
    printf 'Plan preview:\n'
    cat "$TMP_DIR/preview.json"
    ;;
  plan)
    mim_assert_plan_create_path "$SCRIPT_DIR" "$PLAN_OUT"
    write_reviewed_plan "$PLAN_OUT"
    printf 'Wrote reviewed plan to %s\n' "$PLAN_OUT"
    ;;
  apply)
    mim_assert_plan_read_path "$SCRIPT_DIR" "$PLAN_FILE"
    validate_reviewed_plan "$PLAN_FILE"
    apply_reviewed_actions "$PLAN_FILE"
    printf 'Applied reviewed plan.\n'
    ;;
esac
