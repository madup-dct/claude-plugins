#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"
. "$SCRIPT_DIR/cloudflare_api.sh"

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

validate_secret_value() {
  local value=$1
  [[ -n "$value" ]] || return 1
  MIM_SECRET_CANDIDATE="$value" python3 - <<'PY' >/dev/null
import os
value = os.environ["MIM_SECRET_CANDIDATE"].encode("utf-8")
if b"\x00" in value or b"\r" in value or b"\n" in value:
    raise SystemExit(1)
if len(value) < 32:
    raise SystemExit(1)
PY
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
EXPECTED_PATH="$SCRIPT_DIR/.state/task18-edge-expected-$$.json"
POST_APPLY_PATH="$SCRIPT_DIR/.state/task18-edge-post-apply-$$.json"
trap 'rm -rf "$TMP_DIR" "$SNAPSHOT_DIR"; rm -f "$EXPECTED_PATH" "$EXPECTED_PATH.sha256" "$POST_APPLY_PATH" "$POST_APPLY_PATH.sha256"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
command -v wrangler >/dev/null 2>&1 || mim_task18_fail "wrangler CLI is required"

MIM_CONFIG_FILE="$SNAPSHOT_CONFIG" \
MIM_TASK18_PLAN_GENERATED_AT="$PLAN_GENERATED_AT" \
MIM_TASK18_PLAN_EXPIRES_AT="$PLAN_EXPIRES_AT" \
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
import json
import sys
from pathlib import Path

SEP = "\x1f"

for action in json.loads(Path(sys.argv[1]).read_text())["actions"]:
    kind = action["kind"]
    if kind in {"upsert_access_app", "upsert_access_policy"}:
        print(SEP.join([
            kind,
            action["hostname"],
            "" if action.get("app_id") is None else str(action["app_id"]),
            "" if action.get("policy_id") is None else str(action["policy_id"]),
            json.dumps(action["payload"], sort_keys=True, separators=(",", ":")),
            json.dumps(action.get("before_state", {}), sort_keys=True, separators=(",", ":")),
        ]))
    elif kind == "deploy_worker":
        print(SEP.join([
            kind,
            action["worker_name"],
            action["control_hostname"],
            action["app_host_suffix"],
            action["control_origin_url"],
            action["app_gateway_origin_url"],
            action["control_origin_hmac_key_id"],
            action["app_gateway_origin_hmac_key_id"],
            action["project_number"],
            "|".join(action["required_secret_names"]),
            "|".join(action["control_allowed_routes"]),
            json.dumps(action.get("before_state", {}), sort_keys=True, separators=(",", ":")),
        ]))
    elif kind == "upsert_dns_record":
        print(SEP.join([kind, action["hostname"], action["target_hostname"], str(action["proxied"]).lower(), json.dumps(action.get("before_state", {}), sort_keys=True, separators=(",", ":"))]))
    elif kind == "upsert_worker_route":
        print(SEP.join([kind, action["pattern"], action["worker_name"], json.dumps(action.get("before_state", {}), sort_keys=True, separators=(",", ":"))]))
PY

create_worker_config() {
  local out_path=$1
  local control_hostname=$2
  local app_host_suffix=$3
  local control_origin=$4
  local app_gateway_origin=$5
  local control_key_id=$6
  local app_key_id=$7
  local project_number=$8
  local allowed_routes=$9
  python3 - "$SCRIPT_DIR/../../edge/worker/wrangler.jsonc" "$out_path" "$control_hostname" "$app_host_suffix" "$control_origin" "$app_gateway_origin" "$control_key_id" "$app_key_id" "$project_number" "$allowed_routes" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text()
config = json.loads(source)
config["vars"]["MIM_CONTROL_PUBLIC_HOSTNAME"] = sys.argv[3]
config["vars"]["MIM_APP_HOST_SUFFIX"] = sys.argv[4]
config["vars"]["MIM_CONTROL_ORIGIN"] = sys.argv[5]
config["vars"]["MIM_APP_GATEWAY_ORIGIN"] = sys.argv[6]
config["vars"]["MIM_CONTROL_ORIGIN_HMAC_KEY_ID"] = sys.argv[7]
config["vars"]["MIM_APP_GATEWAY_ORIGIN_HMAC_KEY_ID"] = sys.argv[8]
config["vars"]["MIM_PROJECT_NUMBER"] = sys.argv[9]
config["vars"]["MIM_CONTROL_ALLOWED_ROUTES"] = sys.argv[10].replace("|", "\n")
config["secrets"] = {"required": ["MIM_CONTROL_ORIGIN_HMAC_SECRET", "MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET"]}
Path(sys.argv[2]).write_text(json.dumps(config, indent=2) + "\n")
PY
}

write_worker_secret_env() {
  local out_path=$1
  validate_secret_value "${MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET:-}" || mim_task18_fail "MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET must be present and at least 32 bytes"
  validate_secret_value "${MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET:-}" || mim_task18_fail "MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET must be present and at least 32 bytes"
  {
    printf 'MIM_CONTROL_ORIGIN_HMAC_SECRET=%s\n' "$MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET"
    printf 'MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET=%s\n' "$MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET"
  } >"$out_path"
  chmod 600 "$out_path"
}

assert_missing_before_state() {
  local before_state_json=$1
  python3 - "$before_state_json" <<'PY'
import json, sys
if json.loads(sys.argv[1]) != {"status": "missing"}:
    raise SystemExit("Action before_state must remain the reviewed missing sentinel")
PY
}

upsert_access_app() {
  local hostname=$1
  local app_id=$2
  local payload_json=$3
  local before_state_json=$4
  local payload_file="$TMP_DIR/app-payload-$(printf '%s' "$hostname" | tr '*./' '_').json"
  local response_file="$TMP_DIR/app-response-$(printf '%s' "$hostname" | tr '*./' '_').json"
  assert_missing_before_state "$before_state_json"
  [[ -z "$app_id" ]] || mim_task18_fail "Reviewed access app action must not update an existing app"
  printf '%s\n' "$payload_json" >"$payload_file"
  mim_cf_create_access_app_json "$payload_file" >"$response_file"
  python3 - "$response_file" "$TMP_DIR/app-map.json" "$hostname" <<'PY'
import json
import sys
from pathlib import Path

response = json.loads(Path(sys.argv[1]).read_text())["result"]
mapping_path = Path(sys.argv[2])
mapping = {}
if mapping_path.exists():
    mapping = json.loads(mapping_path.read_text())
mapping[sys.argv[3]] = {"id": response.get("id"), "aud": response.get("aud", "")}
mapping_path.write_text(json.dumps(mapping, sort_keys=True) + "\n")
PY
  local created_id
  created_id=$(python3 - "$response_file" <<'PY'
import json, sys
from pathlib import Path
print((json.loads(Path(sys.argv[1]).read_text())["result"]).get("id", ""))
PY
)
  [[ -n "$created_id" ]] || mim_task18_fail "Access app create did not return an app ID"
  mim_cf_get_access_app_json "$created_id" >"$TMP_DIR/app-readback-$(printf '%s' "$hostname" | tr '*./' '_').json"
}

resolve_app_id() {
  local hostname=$1
  local explicit_id=$2
  if [[ -n "$explicit_id" ]]; then
    printf '%s' "$explicit_id"
    return
  fi
  python3 - "$TMP_DIR/app-map.json" "$hostname" <<'PY'
import json
import sys
from pathlib import Path

mapping = json.loads(Path(sys.argv[1]).read_text()) if Path(sys.argv[1]).exists() else {}
print((mapping.get(sys.argv[2]) or {}).get("id", ""))
PY
}

upsert_access_policy() {
  local hostname=$1
  local app_id=$2
  local policy_id=$3
  local payload_json=$4
  local before_state_json=$5
  local resolved_app_id
  local payload_file="$TMP_DIR/policy-payload-$(printf '%s' "$hostname" | tr '*./' '_').json"
  assert_missing_before_state "$before_state_json"
  resolved_app_id=$(resolve_app_id "$hostname" "$app_id")
  [[ -n "$resolved_app_id" ]] || mim_task18_fail "Unable to resolve Access app ID for $hostname"
  [[ -z "$policy_id" ]] || mim_task18_fail "Reviewed access policy action must not update an existing policy"
  printf '%s\n' "$payload_json" >"$payload_file"
  mim_cf_create_access_policy_json "$resolved_app_id" "$payload_file" >"$TMP_DIR/policy-response-$(printf '%s' "$hostname" | tr '*./' '_').json"
}

create_dns_record() {
  local hostname=$1
  local target_hostname=$2
  local proxied=$3
  local before_state_json=$4
  local payload_file="$TMP_DIR/dns-payload-$(printf '%s' "$hostname" | tr '*./' '_').json"
  assert_missing_before_state "$before_state_json"
  python3 - "$payload_file" "$hostname" "$target_hostname" "$proxied" <<'PY'
import json
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({"type":"CNAME","name":sys.argv[2],"content":sys.argv[3],"proxied":sys.argv[4].lower()=="true","ttl":1,"comment":"Managed by Madup Infra Manager"}, sort_keys=True) + "\n")
PY
  mim_cf_create_dns_cname_json "$payload_file" >"$TMP_DIR/dns-response-$(printf '%s' "$hostname" | tr '*./' '_').json"
}

create_worker_route() {
  local pattern=$1
  local worker_name=$2
  local before_state_json=$3
  local payload_file="$TMP_DIR/route-payload-$(printf '%s' "$pattern" | tr '*./' '_').json"
  assert_missing_before_state "$before_state_json"
  python3 - "$payload_file" "$pattern" "$worker_name" <<'PY'
import json
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({"pattern": sys.argv[2], "script": sys.argv[3]}, sort_keys=True) + "\n")
PY
  mim_cf_create_worker_route_json "$payload_file" >"$TMP_DIR/route-response-$(printf '%s' "$pattern" | tr '*./' '_').json"
}

while IFS=$'\x1f' read -r kind a b c d e f g h i j k; do
  [[ -n "$kind" ]] || continue
  case "$kind" in
    upsert_access_app)
      mim_task18_load_config "$SNAPSHOT_CONFIG"
      upsert_access_app "$a" "$b" "$d" "$e"
      ;;
    upsert_access_policy)
      mim_task18_load_config "$SNAPSHOT_CONFIG"
      upsert_access_policy "$a" "$b" "$c" "$d" "$e"
      ;;
    deploy_worker)
      WORKER_CONFIG="$TMP_DIR/wrangler.edge.json"
      SECRET_ENV="$TMP_DIR/wrangler.secrets.env"
      create_worker_config "$WORKER_CONFIG" "$b" "$c" "$d" "$e" "$f" "$g" "$h" "$j"
      python3 - "$k" <<'PY'
import json, sys
state = json.loads(sys.argv[1])
allowed = {"worker_status", "worker_secret_binding_status"}
if set(state) - allowed:
    raise SystemExit("Worker before_state contains unexpected fields")
PY
      write_worker_secret_env "$SECRET_ENV"
      (
        cd "$SCRIPT_DIR/../../edge/worker"
        wrangler deploy --config "$WORKER_CONFIG" --secrets-file "$SECRET_ENV"
      )
      ;;
    upsert_dns_record)
      mim_task18_load_config "$SNAPSHOT_CONFIG"
      create_dns_record "$a" "$b" "$c" "$d"
      ;;
    upsert_worker_route)
      mim_task18_load_config "$SNAPSHOT_CONFIG"
      create_worker_route "$a" "$b" "$c"
      ;;
    *)
      mim_task18_fail "Unknown reviewed action: $kind"
      ;;
  esac
done <"$TMP_DIR/actions.tsv"

if [[ -f "$TMP_DIR/app-map.json" ]]; then
  python3 - "$TMP_DIR/app-map.json" <<'PY'
import json
import sys
from pathlib import Path

mapping = json.loads(Path(sys.argv[1]).read_text())
control = (mapping.get("mim.madup.app") or {}).get("aud", "")
wildcard = (mapping.get("*.madup.app") or {}).get("aud", "")
if control and wildcard and control == wildcard:
    raise SystemExit("Access app readback audiences must remain distinct after apply")
PY
fi

MIM_CONFIG_FILE="$SNAPSHOT_CONFIG" \
bash "$SCRIPT_DIR/plan.sh" --plan --out "$POST_APPLY_PATH" >/dev/null
python3 - "$POST_APPLY_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
required = {
    "control_access_app_status": "ready",
    "control_access_policy_status": "ready",
    "wildcard_access_app_status": "ready",
    "wildcard_access_policy_status": "ready",
    "github_webhook_access_app_status": "ready",
    "github_webhook_access_policy_status": "ready",
    "control_dns_record_status": "ready",
    "wildcard_dns_record_status": "ready",
    "control_worker_route_status": "ready",
    "wildcard_worker_route_status": "ready",
    "worker_secret_binding_status": "ready",
    "audience_distinct_status": "ready",
}
for key, expected in required.items():
    if plan["initial_state"].get(key) != expected:
        raise SystemExit(f"Post-apply readback for {key} did not match the reviewed ready state")
if plan.get("status") != "ready":
    raise SystemExit("Post-apply plan contains blockers")
PY

printf 'Applied reviewed plan.\n'
