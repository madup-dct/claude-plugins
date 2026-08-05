#!/usr/bin/env bash

mim_cf_require_api_access() {
  command -v curl >/dev/null 2>&1 || mim_task18_fail "curl is required"
  command -v python3 >/dev/null 2>&1 || mim_task18_fail "python3 is required"
  [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]] || mim_task18_fail "CLOUDFLARE_API_TOKEN is required"
}

mim_cf_validate_api_token() {
  local token=${CLOUDFLARE_API_TOKEN:-}
  local LC_ALL=C
  local sanitized
  [[ -n "$token" ]] || mim_task18_fail "CLOUDFLARE_API_TOKEN is required"
  case "$token" in
    *\"*|*\\*)
      mim_task18_fail "CLOUDFLARE_API_TOKEN contains unsupported characters"
      ;;
  esac
  sanitized=${token//[$'\001'-$'\037'$'\177']/}
  if [[ ${#sanitized} -ne ${#token} ]]; then
    mim_task18_fail "CLOUDFLARE_API_TOKEN contains unsupported characters"
  fi
}

mim_cf_api_call() {
  local method=$1
  local path=$2
  local payload_file=${3:-}
  local response_file curl_config_file

  mim_cf_require_api_access
  mim_cf_validate_api_token
  response_file=$(mktemp)
  curl_config_file=$(mktemp)
  (
    cleanup() {
      rm -f "${response_file:-}" "${curl_config_file:-}"
    }
    trap cleanup EXIT INT TERM

    {
      printf 'silent\n'
      printf 'show-error\n'
      printf 'header = "Authorization: Bearer %s"\n' "$CLOUDFLARE_API_TOKEN"
      if [[ -n "$payload_file" ]]; then
        printf 'header = "Content-Type: application/json"\n'
      fi
    } >"$curl_config_file"
    chmod 600 "$curl_config_file"

    if [[ -n "$payload_file" ]]; then
      http_code=$(curl -sS --config "$curl_config_file" -X "$method" \
        --data-binary "@$payload_file" \
        -o "$response_file" \
        -w '%{http_code}' \
        "https://api.cloudflare.com/client/v4$path")
    else
      http_code=$(curl -sS --config "$curl_config_file" -X "$method" \
        -o "$response_file" \
        -w '%{http_code}' \
        "https://api.cloudflare.com/client/v4$path")
    fi

    python_status=0
    if python3 - "$response_file" "$http_code" "$method" "$path" <<'PY'
import json
import sys
from pathlib import Path

body = Path(sys.argv[1]).read_text()
status = sys.argv[2]
method = sys.argv[3]
path = sys.argv[4]
prefix = f"Cloudflare API {method} {path} failed"

try:
    data = json.loads(body)
except json.JSONDecodeError as exc:
    raise SystemExit(f"{prefix}: invalid JSON response ({exc})")

if not status.startswith("2"):
    errors = data.get("errors") or []
    detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else f"HTTP {status}"
    raise SystemExit(f"{prefix}: {detail}")

if data.get("success") is not True:
    errors = data.get("errors") or []
    detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else "success=false"
    raise SystemExit(f"{prefix}: {detail}")

print(body)
PY
    then
      python_status=0
    else
      python_status=$?
    fi
    cleanup
    return "$python_status"
  )
}

mim_cf_get_zone_json() {
  mim_cf_api_call GET "/zones/$MIM_CLOUDFLARE_ZONE_ID"
}

mim_cf_get_organization_json() {
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/organizations"
}

mim_cf_list_identity_providers_json() {
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/identity_providers?per_page=1000"
}

mim_cf_list_access_apps_json() {
  local page=1 total_pages=1 combined='[]' page_json page_meta page_total page_result_json
  while (( page <= total_pages )); do
    page_json=$(mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps?page=$page&per_page=1000")
    page_meta=$(python3 - "$page_json" "$page" <<'PY'
import json
import sys

data = json.loads(sys.argv[1])
page = int(sys.argv[2])
result_info = data.get("result_info")
if not isinstance(result_info, dict):
    raise SystemExit(f"Cloudflare Access apps page {page} is missing pagination metadata")
raw_total_pages = result_info.get("total_pages")
try:
    total_pages = int(raw_total_pages)
except (TypeError, ValueError):
    raise SystemExit(f"Cloudflare Access apps page {page} has invalid pagination metadata")
if total_pages < 1:
    raise SystemExit(f"Cloudflare Access apps page {page} has invalid pagination metadata")
result = data.get("result")
if not isinstance(result, list):
    raise SystemExit(f"Cloudflare Access apps page {page} has invalid result payload")
print(f"{total_pages}\t{json.dumps(result, separators=(',', ':'), sort_keys=True)}")
PY
)
    IFS=$'\t' read -r page_total page_result_json <<<"$page_meta"
    if (( page == 1 )); then
      total_pages=$page_total
    elif [[ "$page_total" -ne "$total_pages" ]]; then
      mim_task18_fail "Cloudflare Access apps pagination metadata changed between pages"
    fi
    combined=$(python3 - "$combined" "$page_result_json" <<'PY'
import json
import sys

combined = json.loads(sys.argv[1])
page_result = json.loads(sys.argv[2])
if not isinstance(combined, list) or not isinstance(page_result, list):
    raise SystemExit(1)
print(json.dumps(combined + page_result, separators=(",", ":"), sort_keys=True))
PY
)
    page=$((page + 1))
  done
  python3 - "$combined" "$total_pages" <<'PY'
import json
import sys

result = json.loads(sys.argv[1])
total_pages = int(sys.argv[2])
print(json.dumps({
    "success": True,
    "errors": [],
    "messages": [],
    "result": result,
    "result_info": {"total_pages": total_pages},
}, sort_keys=True))
PY
}

mim_cf_get_access_app_json() {
  local app_id=$1
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps/$app_id"
}

mim_cf_create_access_app_json() {
  local payload_file=$1
  mim_cf_api_call POST "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps" "$payload_file"
}

mim_cf_update_access_app_json() {
  local app_id=$1
  local payload_file=$2
  mim_cf_api_call PUT "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps/$app_id" "$payload_file"
}

mim_cf_list_access_policies_json() {
  local app_id=$1
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps/$app_id/policies?per_page=1000"
}

mim_cf_create_access_policy_json() {
  local app_id=$1
  local payload_file=$2
  mim_cf_api_call POST "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps/$app_id/policies" "$payload_file"
}

mim_cf_get_access_policy_json() {
  local app_id=$1
  local policy_id=$2
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps/$app_id/policies/$policy_id"
}

mim_cf_update_access_policy_json() {
  local app_id=$1
  local policy_id=$2
  local payload_file=$3
  mim_cf_api_call PUT "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/apps/$app_id/policies/$policy_id" "$payload_file"
}

mim_cf_list_access_users_json() {
  local page=$1
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/access/users?page=$page&per_page=100"
}

mim_cf_count_access_seats() {
  local page total_pages seat_count
  page=1
  total_pages=1
  seat_count=0
  while (( page <= total_pages )); do
    local page_json
    page_json=$(mim_cf_list_access_users_json "$page")
    read -r total_pages page_count < <(python3 - "$page_json" <<'PY'
import json
import sys

data = json.loads(sys.argv[1])
result_info = data.get("result_info") or {}
total_pages = int(result_info.get("total_pages") or 1)
seat_count = sum(1 for item in data.get("result") or [] if item.get("access_seat") is True)
print(total_pages, seat_count)
PY
)
    seat_count=$((seat_count + page_count))
    page=$((page + 1))
  done
  printf '%s\n' "$seat_count"
}

mim_cf_list_dns_records_json() {
  local hostname=$1
  mim_cf_api_call GET "/zones/$MIM_CLOUDFLARE_ZONE_ID/dns_records?type=CNAME&name=$hostname"
}

mim_cf_list_worker_routes_json() {
  mim_cf_api_call GET "/zones/$MIM_CLOUDFLARE_ZONE_ID/workers/routes"
}

mim_cf_list_worker_scripts_json() {
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/workers/scripts"
}

mim_cf_list_worker_secrets_json() {
  local script_name=$1
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/workers/scripts/$script_name/secrets"
}

mim_cf_get_worker_secret_json() {
  local script_name=$1
  local secret_name=$2
  mim_cf_api_call GET "/accounts/$MIM_CLOUDFLARE_ACCOUNT_ID/workers/scripts/$script_name/secrets/$secret_name"
}

mim_cf_upsert_dns_cname() {
  local hostname=$1
  local target_hostname=$2
  local proxied=$3
  local existing_json existing_count record_id current_content current_proxied payload_file

  existing_json=$(mim_cf_list_dns_records_json "$hostname")
  existing_count=$(python3 - "$existing_json" <<'PY'
import json
import sys
print(len(json.loads(sys.argv[1])["result"]))
PY
)
  if [[ "$existing_count" -gt 1 ]]; then
    mim_task18_fail "Expected at most one CNAME record for $hostname"
  fi

  payload_file=$(mktemp)
  python3 - "$payload_file" "$hostname" "$target_hostname" "$proxied" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "type": "CNAME",
    "name": sys.argv[2],
    "content": sys.argv[3],
    "proxied": sys.argv[4].lower() == "true",
    "ttl": 1,
    "comment": "Managed by Madup Infra Manager",
}, sort_keys=True) + "\n")
PY

  if [[ "$existing_count" -eq 0 ]]; then
    mim_cf_api_call POST "/zones/$MIM_CLOUDFLARE_ZONE_ID/dns_records" "$payload_file" >/dev/null
    rm -f "$payload_file"
    return
  fi

  read -r record_id current_content current_proxied < <(python3 - "$existing_json" <<'PY'
import json
import sys
record = json.loads(sys.argv[1])["result"][0]
print(record["id"], record.get("content", ""), str(record.get("proxied", False)).lower())
PY
)

  if [[ "$current_content" == "$target_hostname" && "$current_proxied" == "${proxied,,}" ]]; then
    rm -f "$payload_file"
    return
  fi

  mim_cf_api_call PUT "/zones/$MIM_CLOUDFLARE_ZONE_ID/dns_records/$record_id" "$payload_file" >/dev/null
  rm -f "$payload_file"
}

mim_cf_create_dns_cname_json() {
  local payload_file=$1
  mim_cf_api_call POST "/zones/$MIM_CLOUDFLARE_ZONE_ID/dns_records" "$payload_file"
}

mim_cf_get_dns_record_json() {
  local record_id=$1
  mim_cf_api_call GET "/zones/$MIM_CLOUDFLARE_ZONE_ID/dns_records/$record_id"
}

mim_cf_upsert_worker_route() {
  local pattern=$1
  local script_name=$2
  local existing_json existing_count route_id current_script payload_file

  existing_json=$(mim_cf_list_worker_routes_json)
  existing_count=$(python3 - "$existing_json" "$pattern" <<'PY'
import json
import sys
routes = json.loads(sys.argv[1])["result"]
pattern = sys.argv[2]
print(sum(1 for route in routes if route.get("pattern") == pattern))
PY
)
  if [[ "$existing_count" -gt 1 ]]; then
    mim_task18_fail "Expected at most one Worker route for $pattern"
  fi

  payload_file=$(mktemp)
  python3 - "$payload_file" "$pattern" "$script_name" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "pattern": sys.argv[2],
    "script": sys.argv[3],
}, sort_keys=True) + "\n")
PY

  if [[ "$existing_count" -eq 0 ]]; then
    mim_cf_api_call POST "/zones/$MIM_CLOUDFLARE_ZONE_ID/workers/routes" "$payload_file" >/dev/null
    rm -f "$payload_file"
    return
  fi

  read -r route_id current_script < <(python3 - "$existing_json" "$pattern" <<'PY'
import json
import sys
pattern = sys.argv[2]
for route in json.loads(sys.argv[1])["result"]:
    if route.get("pattern") == pattern:
        print(route["id"], route.get("script", ""))
        break
PY
)

  if [[ "$current_script" == "$script_name" ]]; then
    rm -f "$payload_file"
    return
  fi

  mim_cf_api_call PUT "/zones/$MIM_CLOUDFLARE_ZONE_ID/workers/routes/$route_id" "$payload_file" >/dev/null
  rm -f "$payload_file"
}

mim_cf_create_worker_route_json() {
  local payload_file=$1
  mim_cf_api_call POST "/zones/$MIM_CLOUDFLARE_ZONE_ID/workers/routes" "$payload_file"
}

mim_cf_get_worker_route_json() {
  local route_id=$1
  mim_cf_api_call GET "/zones/$MIM_CLOUDFLARE_ZONE_ID/workers/routes/$route_id"
}
