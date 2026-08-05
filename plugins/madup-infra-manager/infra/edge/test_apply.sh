#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
APPLY_SCRIPT="$SCRIPT_DIR/apply.sh"
TEST_LIB="$SCRIPT_DIR/../release/test_task18_lib.sh"
. "$TEST_LIB"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"; rm -f "$SCRIPT_DIR"/.state/test-edge-apply*.json "$SCRIPT_DIR"/.state/test-edge-apply*.json.sha256 2>/dev/null || true' EXIT
mkdir -p "$SCRIPT_DIR/.state"

CURL_LOG="$TMP_DIR/curl.log"
WRANGLER_LOG="$TMP_DIR/wrangler.log"
WRANGLER_CAPTURE="$TMP_DIR/wrangler-config.json"
STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

method=GET
output_file=
header_file=
config_file=
url=
data_file=
http_code=200

for arg in "$@"; do
  if [[ -n "${CLOUDFLARE_API_TOKEN:-}" && "$arg" == *"$CLOUDFLARE_API_TOKEN"* ]]; then
    printf 'curl argv leaked the Cloudflare token\n' >&2
    exit 96
  fi
done

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -X) method=$2; shift 2 ;;
    -o) output_file=$2; shift 2 ;;
    -D) header_file=$2; shift 2 ;;
    -w) shift 2 ;;
    -H) shift 2 ;;
    --config) config_file=$2; shift 2 ;;
    --data-binary) data_file=${2#@}; shift 2 ;;
    http*) url=$1; shift ;;
    *) shift ;;
  esac
done

[[ -n "$config_file" && -r "$config_file" ]] || { printf 'missing curl config file\n' >&2; exit 97; }
python3 - "$config_file" <<'PY' >/dev/null
import os
import sys

mode = os.stat(sys.argv[1]).st_mode & 0o777
if mode != 0o600:
    raise SystemExit(98)
PY
grep -F "Authorization: Bearer ${CLOUDFLARE_API_TOKEN:?}" "$config_file" >/dev/null
if [[ -n "$data_file" ]]; then
  grep -F 'Content-Type: application/json' "$config_file" >/dev/null
fi

payload=
if [[ -n "$data_file" && -f "$data_file" ]]; then
  payload=$(cat "$data_file")
fi
printf '%s\t%s\t%s\t%s\t%s\n' "$method" "$url" "$config_file" "$output_file" "$http_code" >> "${CURL_LOG:?}"
[[ -n "$header_file" ]] && : >"$header_file"

body=$(METHOD="$method" URL="$url" PAYLOAD="$payload" python3 - <<'PY'
import json
import os
from urllib.parse import parse_qs, urlparse

account_id = os.environ["TASK18_CLOUDFLARE_ACCOUNT_ID"]
zone_id = os.environ["TASK18_CLOUDFLARE_ZONE_ID"]
team_name = os.environ["TASK18_CLOUDFLARE_TEAM_NAME"]
hostname = "mim.madup.app"
wildcard_hostname = "*.madup.app"
webhook_hostname = "mim.madup.app/v1/webhooks/github"
allowed_group = "mim-users@madup.com"
control_target = os.environ.get("CF_CONTROL_DNS_TARGET", "mim-control-plane-123456.asia-northeast3.run.app")
wildcard_target = os.environ.get("CF_WILDCARD_DNS_TARGET", "mim-app-gateway-123456.asia-northeast3.run.app")
worker_name = "madup-infra-manager-edge-worker"
control_app_id = "control-app-123"
wildcard_app_id = "wildcard-app-456"
webhook_app_id = "webhook-app-789"
google_idp_id = "google-idp-1"

state_dir = os.environ["CF_STATE_DIR"]
apps_file = os.path.join(state_dir, "apps.json")
policies_file = os.path.join(state_dir, "policies.json")
dns_file = os.path.join(state_dir, "dns.json")
routes_file = os.path.join(state_dir, "routes.json")
secrets_file = os.path.join(state_dir, "secrets.json")

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)

def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)

def ok(result, result_info=None):
    payload = {"success": True, "errors": [], "messages": [], "result": result}
    if result_info is not None:
        payload["result_info"] = result_info
    return json.dumps(payload)

apps = load_json(apps_file, {})
policies = load_json(policies_file, {
    control_app_id: [],
    wildcard_app_id: [],
    webhook_app_id: [],
})
dns = load_json(dns_file, {})
routes = load_json(routes_file, {})
secrets = load_json(secrets_file, [])

def policy_payload(policy_id):
    return {
        "id": policy_id,
        "name": "mim-users-allow",
        "decision": "allow",
        "include": [{"gsuite": {"email": allowed_group, "identity_provider_id": google_idp_id}}],
        "require": [],
        "exclude": [],
        "precedence": 1,
        "created_at": "2026-08-05T00:00:00Z",
        "updated_at": "2026-08-05T00:00:00Z",
    }

def webhook_app_payload():
    return {
        "id": webhook_app_id,
        "domain": webhook_hostname,
        "name": f"{webhook_hostname} access",
        "type": "self_hosted",
        "app_launcher_visible": False,
        "allow_iframe": False,
        "skip_interstitial": False,
        "destinations": [],
        "policies": [],
        "created_at": "2026-08-05T00:00:00Z",
        "updated_at": "2026-08-05T00:00:00Z",
    }

def webhook_policy_payload(policy_id):
    return {
        "id": policy_id,
        "name": "github-webhook-bypass",
        "decision": "bypass",
        "include": [{"everyone": {}}],
        "require": [],
        "exclude": [],
        "precedence": 1,
        "created_at": "2026-08-05T00:00:00Z",
        "updated_at": "2026-08-05T00:00:00Z",
    }

control_mode = os.environ.get("CF_CONTROL_APP_MODE", "missing")
wildcard_mode = os.environ.get("CF_WILDCARD_APP_MODE", "missing")
webhook_mode = os.environ.get("CF_WEBHOOK_APP_MODE", "missing")
control_policy_mode = os.environ.get("CF_CONTROL_POLICY_MODE", "missing")
wildcard_policy_mode = os.environ.get("CF_WILDCARD_POLICY_MODE", "missing")
webhook_policy_mode = os.environ.get("CF_WEBHOOK_POLICY_MODE", "missing")

if not apps:
    if control_mode != "missing":
        control_aud = "control-audience-12345678"
        if control_mode == "same_aud":
            control_aud = "shared-audience-12345678"
        apps[hostname] = {
            "id": control_app_id,
            "domain": hostname,
            "type": "self_hosted",
            "allowed_idps": [google_idp_id],
            "auto_redirect_to_identity": True,
            "aud": control_aud,
            "session_duration": "168h",
            "allow_iframe": False,
            "skip_interstitial": False,
            "destinations": [],
            "policies": [],
            "created_at": "2026-08-05T00:00:00Z",
            "updated_at": "2026-08-05T00:00:00Z",
            "oauth_configuration": {
                "enabled": True,
                "dynamic_client_registration": {
                    "enabled": True,
                    "allow_any_on_localhost": True,
                    "allow_any_on_loopback": True,
                    "allowed_uris": [],
                },
                "grant": {"access_token_lifetime": "15m", "session_duration": "168h"},
            },
        }
    if wildcard_mode != "missing":
        wildcard_aud = "wildcard-audience-87654321"
        if wildcard_mode == "same_aud":
            wildcard_aud = "shared-audience-12345678"
        apps[wildcard_hostname] = {
            "id": wildcard_app_id,
            "domain": wildcard_hostname,
            "type": "self_hosted",
            "allowed_idps": [google_idp_id],
            "auto_redirect_to_identity": True,
            "aud": wildcard_aud,
            "session_duration": "168h",
            "allow_iframe": False,
            "skip_interstitial": False,
            "destinations": [],
            "policies": [],
            "created_at": "2026-08-05T00:00:00Z",
            "updated_at": "2026-08-05T00:00:00Z",
        }
    if webhook_mode != "missing":
        apps[webhook_hostname] = webhook_app_payload()
    save_json(apps_file, apps)

if policies == {control_app_id: [], wildcard_app_id: [], webhook_app_id: []}:
    if control_policy_mode != "missing" and control_mode != "missing":
        policies[control_app_id] = [policy_payload("control-policy-1")]
    if wildcard_policy_mode != "missing" and wildcard_mode != "missing":
        policies[wildcard_app_id] = [policy_payload("wildcard-policy-1")]
    if webhook_policy_mode != "missing" and webhook_mode != "missing":
        if webhook_policy_mode == "extra":
            policies[webhook_app_id] = [webhook_policy_payload("webhook-policy-1"), webhook_policy_payload("webhook-policy-2")]
        else:
            policies[webhook_app_id] = [webhook_policy_payload("webhook-policy-1")]
    save_json(policies_file, policies)

parsed = urlparse(os.environ["URL"])
path = parsed.path.split("/client/v4", 1)[1]
query = parse_qs(parsed.query)
method = os.environ["METHOD"]
payload_text = os.environ.get("PAYLOAD", "")
payload_data = json.loads(payload_text) if payload_text else {}

if method == "GET" and path == f"/zones/{zone_id}":
    print(ok({"id": zone_id, "name": "madup.app", "status": "active", "account": {"id": account_id}, "name_servers": ["mina.ns.cloudflare.com", "pete.ns.cloudflare.com"]}))
elif method == "GET" and path == f"/accounts/{account_id}/access/organizations":
    print(ok({"auth_domain": f"{team_name}.cloudflareaccess.com"}))
elif method == "GET" and path == f"/accounts/{account_id}/access/identity_providers":
    print(ok([{"id": google_idp_id, "type": "google-apps"}]))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps":
    page = int(query.get("page", ["1"])[0])
    access_apps_total_pages = int(os.environ.get("CF_ACCESS_APPS_TOTAL_PAGES", "1"))
    access_apps_page2_mode = os.environ.get("CF_ACCESS_APPS_PAGE2_MODE", "none")
    access_apps_metadata_mode = os.environ.get("CF_ACCESS_APPS_METADATA_MODE", "ready")
    access_apps_page2_total_pages = int(os.environ.get("CF_ACCESS_APPS_PAGE2_TOTAL_PAGES", str(access_apps_total_pages)))

    def page_apps(page_number):
        if page_number == 1:
            result = []
            for domain in (hostname, wildcard_hostname, webhook_hostname):
                if domain in apps:
                    result.append(apps[domain])
            return result
        if page_number == 2:
            if access_apps_page2_mode == "error":
                raise SystemExit("simulated access apps page 2 failure")
            if access_apps_page2_mode == "duplicate_control":
                return [{
                    "id": "control-app-dup",
                    "domain": hostname,
                    "type": "self_hosted",
                    "allowed_idps": [google_idp_id],
                    "auto_redirect_to_identity": True,
                    "aud": "control-audience-dup",
                    "session_duration": "168h",
                    "allow_iframe": False,
                    "skip_interstitial": False,
                    "destinations": [],
                    "policies": [],
                    "created_at": "2026-08-05T00:00:00Z",
                    "updated_at": "2026-08-05T00:00:00Z",
                    "oauth_configuration": {
                        "enabled": True,
                        "dynamic_client_registration": {
                            "enabled": True,
                            "allow_any_on_localhost": True,
                            "allow_any_on_loopback": True,
                            "allowed_uris": [],
                        },
                        "grant": {"access_token_lifetime": "15m", "session_duration": "168h"},
                    },
                }]
            if access_apps_page2_mode == "duplicate_wildcard":
                return [{
                    "id": "wildcard-app-dup",
                    "domain": wildcard_hostname,
                    "type": "self_hosted",
                    "allowed_idps": [google_idp_id],
                    "auto_redirect_to_identity": True,
                    "aud": "wildcard-audience-dup",
                    "session_duration": "168h",
                    "allow_iframe": False,
                    "skip_interstitial": False,
                    "destinations": [],
                    "policies": [],
                    "created_at": "2026-08-05T00:00:00Z",
                    "updated_at": "2026-08-05T00:00:00Z",
                }]
            if access_apps_page2_mode == "managed_overlap":
                return [{
                    "id": "extra-overlap-app-1",
                    "domain": "docs.madup.app",
                    "name": "madup-infra-manager:docs.madup.app",
                    "type": "self_hosted",
                    "allowed_idps": [google_idp_id],
                    "auto_redirect_to_identity": True,
                    "app_launcher_visible": False,
                    "allow_iframe": False,
                    "skip_interstitial": False,
                    "destinations": [],
                    "policies": [],
                    "created_at": "2026-08-05T00:00:00Z",
                    "updated_at": "2026-08-05T00:00:00Z",
                }]
            if access_apps_page2_mode == "unrelated":
                return [{
                    "id": "extra-unrelated-app-1",
                    "domain": "example.org",
                    "name": "example-org-app",
                    "type": "self_hosted",
                    "allowed_idps": [google_idp_id],
                    "auto_redirect_to_identity": True,
                    "allow_iframe": False,
                    "skip_interstitial": False,
                    "destinations": [],
                    "policies": [],
                    "created_at": "2026-08-05T00:00:00Z",
                    "updated_at": "2026-08-05T00:00:00Z",
                }]
            return []
        return []

    result = page_apps(page)
    if page == 1 and access_apps_metadata_mode == "missing":
        print(json.dumps({"success": True, "errors": [], "messages": [], "result": result}))
    elif page == 1 and access_apps_metadata_mode == "bad_total_pages":
        print(json.dumps({"success": True, "errors": [], "messages": [], "result": result, "result_info": {"total_pages": "two"}}))
    elif page == 2 and access_apps_metadata_mode == "missing":
        print(json.dumps({"success": True, "errors": [], "messages": [], "result": result}))
    else:
        total_pages = access_apps_total_pages if page == 1 else access_apps_page2_total_pages
        print(json.dumps({"success": True, "errors": [], "messages": [], "result": result, "result_info": {"total_pages": total_pages}}))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{control_app_id}":
    print(ok(apps[hostname]))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{wildcard_app_id}":
    print(ok(apps[wildcard_hostname]))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{webhook_app_id}":
    print(ok(apps[webhook_hostname]))
elif method == "POST" and path == f"/accounts/{account_id}/access/apps":
    domain = payload_data["domain"]
    if domain == hostname:
        payload_data["id"] = control_app_id
        payload_data["aud"] = "control-audience-12345678"
    elif domain == wildcard_hostname:
        payload_data["id"] = wildcard_app_id
        payload_data["aud"] = "wildcard-audience-87654321"
    elif domain == webhook_hostname:
        payload_data["id"] = webhook_app_id
    apps[domain] = payload_data
    save_json(apps_file, apps)
    print(ok(payload_data))
elif method == "PUT" and path == f"/accounts/{account_id}/access/apps/{control_app_id}":
    payload_data["id"] = control_app_id
    payload_data["aud"] = "control-audience-12345678"
    apps[hostname] = payload_data
    save_json(apps_file, apps)
    print(ok(payload_data))
elif method == "PUT" and path == f"/accounts/{account_id}/access/apps/{wildcard_app_id}":
    payload_data["id"] = wildcard_app_id
    payload_data["aud"] = "wildcard-audience-87654321"
    apps[wildcard_hostname] = payload_data
    save_json(apps_file, apps)
    print(ok(payload_data))
elif method == "PUT" and path == f"/accounts/{account_id}/access/apps/{webhook_app_id}":
    payload_data["id"] = webhook_app_id
    apps[webhook_hostname] = payload_data
    save_json(apps_file, apps)
    print(ok(payload_data))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{control_app_id}/policies":
    print(ok(policies.get(control_app_id, [])))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{wildcard_app_id}/policies":
    print(ok(policies.get(wildcard_app_id, [])))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{webhook_app_id}/policies":
    print(ok(policies.get(webhook_app_id, [])))
elif method == "POST" and path == f"/accounts/{account_id}/access/apps/{control_app_id}/policies":
    policies[control_app_id] = [policy_payload("control-policy-1")]
    save_json(policies_file, policies)
    print(ok(policies[control_app_id][0]))
elif method == "POST" and path == f"/accounts/{account_id}/access/apps/{wildcard_app_id}/policies":
    policies[wildcard_app_id] = [policy_payload("wildcard-policy-1")]
    save_json(policies_file, policies)
    print(ok(policies[wildcard_app_id][0]))
elif method == "PUT" and path == f"/accounts/{account_id}/access/apps/{control_app_id}/policies/control-policy-1":
    policies[control_app_id] = [policy_payload("control-policy-1")]
    save_json(policies_file, policies)
    print(ok(policies[control_app_id][0]))
elif method == "PUT" and path == f"/accounts/{account_id}/access/apps/{wildcard_app_id}/policies/wildcard-policy-1":
    policies[wildcard_app_id] = [policy_payload("wildcard-policy-1")]
    save_json(policies_file, policies)
    print(ok(policies[wildcard_app_id][0]))
elif method == "POST" and path == f"/accounts/{account_id}/access/apps/{webhook_app_id}/policies":
    policies[webhook_app_id] = [webhook_policy_payload("webhook-policy-1")]
    save_json(policies_file, policies)
    print(ok(policies[webhook_app_id][0]))
elif method == "PUT" and path == f"/accounts/{account_id}/access/apps/{webhook_app_id}/policies/webhook-policy-1":
    policies[webhook_app_id] = [webhook_policy_payload("webhook-policy-1")]
    save_json(policies_file, policies)
    print(ok(policies[webhook_app_id][0]))
elif method == "GET" and path == f"/accounts/{account_id}/access/users":
    print(ok([{"id": "user-1", "email": "user-1@madup.com", "access_seat": True}], {"count": 1, "page": 1, "per_page": 100, "total_count": 1, "total_pages": 1}))
elif method == "GET" and path == f"/zones/{zone_id}/dns_records":
    requested_name = query.get("name", [""])[0]
    result = []
    if requested_name in dns:
      result = [dns[requested_name]]
    print(ok(result))
elif method == "POST" and path == f"/zones/{zone_id}/dns_records":
    payload_data["id"] = "dns-control-1" if payload_data["name"] == hostname else "dns-wildcard-1"
    dns[payload_data["name"]] = payload_data
    save_json(dns_file, dns)
    print(ok(payload_data))
elif method == "PUT" and path == f"/zones/{zone_id}/dns_records/dns-control-1":
    payload_data["id"] = "dns-control-1"
    dns[hostname] = payload_data
    save_json(dns_file, dns)
    print(ok(payload_data))
elif method == "PUT" and path == f"/zones/{zone_id}/dns_records/dns-wildcard-1":
    payload_data["id"] = "dns-wildcard-1"
    dns[wildcard_hostname] = payload_data
    save_json(dns_file, dns)
    print(ok(payload_data))
elif method == "GET" and path == f"/zones/{zone_id}/workers/routes":
    result = list(routes.values())
    print(ok(result))
elif method == "POST" and path == f"/zones/{zone_id}/workers/routes":
    route_id = "route-control-1" if payload_data["pattern"] == f"{hostname}/*" else "route-wildcard-1"
    payload_data["id"] = route_id
    routes[payload_data["pattern"]] = payload_data
    save_json(routes_file, routes)
    print(ok(payload_data))
elif method == "PUT" and path == f"/zones/{zone_id}/workers/routes/route-control-1":
    payload_data["id"] = "route-control-1"
    routes[payload_data["pattern"]] = payload_data
    save_json(routes_file, routes)
    print(ok(payload_data))
elif method == "PUT" and path == f"/zones/{zone_id}/workers/routes/route-wildcard-1":
    payload_data["id"] = "route-wildcard-1"
    routes[payload_data["pattern"]] = payload_data
    save_json(routes_file, routes)
    print(ok(payload_data))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts":
    print(ok([{"id": worker_name}]))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts/{worker_name}/secrets":
    if not secrets:
        print(ok([]))
    else:
        print(ok(secrets))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts/{worker_name}/secrets/MIM_CONTROL_ORIGIN_HMAC_SECRET":
    secret = next((item for item in secrets if item["name"] == "MIM_CONTROL_ORIGIN_HMAC_SECRET"), {})
    print(ok(secret))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts/{worker_name}/secrets/MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET":
    secret = next((item for item in secrets if item["name"] == "MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET"), {})
    print(ok(secret))
else:
    raise SystemExit(f"unexpected curl request: {method} {path}")
PY
)

if [[ "${CF_HTTP_ERROR_MODE:-}" == "error" ]]; then
  body='{"success": false, "errors": [{"message": "simulated api error"}], "messages": [], "result": null}'
  printf '%s' "$body" >"$output_file"
  printf '500'
  exit 0
fi

printf '%s' "$body" >"$output_file"
printf '200'
EOF
chmod +x "$STUB_BIN/curl"

cat >"$STUB_BIN/wrangler" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${WRANGLER_LOG:?}"
config=
secrets_file=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --config) config=$2; shift 2 ;;
    --secrets-file) secrets_file=$2; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$config" && -r "$config" ]] || exit 97
[[ -n "$secrets_file" && -r "$secrets_file" ]] || exit 98
cp "$config" "${WRANGLER_CAPTURE:?}"
python3 - <<'PY'
import json
import os
from pathlib import Path

state_dir = Path(os.environ["CF_STATE_DIR"])
state_dir.mkdir(parents=True, exist_ok=True)
state = [
    {"name": "MIM_CONTROL_ORIGIN_HMAC_SECRET", "type": "secret_text"},
    {"name": "MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET", "type": "secret_text"},
]
(state_dir / "secrets.json").write_text(json.dumps(state, sort_keys=True))
PY
EOF
chmod +x "$STUB_BIN/wrangler"

FAILURES=0
PLAN_PATH="$SCRIPT_DIR/.state/test-edge-apply-ready.json"
CONTROL_SECRET=0123456789abcdef0123456789abcdef
APP_SECRET=fedcba9876543210fedcba9876543210

make_plan() {
  local config_path=$1
  local protected_path=$2
  local state_dir=$3
  rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
  mkdir -p "$state_dir"
  PATH="$STUB_BIN:$PATH" \
    LC_ALL=C \
    LANG=C \
    CURL_LOG="$CURL_LOG" \
    WRANGLER_LOG="$WRANGLER_LOG" \
    WRANGLER_CAPTURE="$WRANGLER_CAPTURE" \
    CF_STATE_DIR="$state_dir" \
    TASK18_CLOUDFLARE_ACCOUNT_ID="$TASK18_CLOUDFLARE_ACCOUNT_ID" \
    TASK18_CLOUDFLARE_ZONE_ID="$TASK18_CLOUDFLARE_ZONE_ID" \
    TASK18_CLOUDFLARE_TEAM_NAME="$TASK18_CLOUDFLARE_TEAM_NAME" \
    CLOUDFLARE_API_TOKEN=test-token \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    MIM_TASK18_EDGE_ORIGIN_URL="https://mim-control-plane-123456.asia-northeast3.run.app" \
    MIM_TASK18_APP_GATEWAY_ORIGIN_URL="https://mim-app-gateway-123456.asia-northeast3.run.app" \
    MIM_TASK18_EDGE_ORIGIN_HMAC_KEY_ID="control-current" \
    MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_KEY_ID="app-current" \
    MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET="$CONTROL_SECRET" \
    MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET="$APP_SECRET" \
    MIM_PROJECT_NUMBER=123456789012 \
    bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >/dev/null
}

run_apply_case() {
  local case_name=$1
  local expected_exit=$2
  local expected_substring=$3
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  local output_path="$TMP_DIR/$case_name.out"
  local state_dir="$TMP_DIR/$case_name.cf-state"
  task18_write_valid_config "$config_path"
  task18_write_protected_file "$protected_path"
  : >"$CURL_LOG"
  : >"$WRANGLER_LOG"
  rm -f "$WRANGLER_CAPTURE"
  rm -rf "$state_dir"
  make_plan "$config_path" "$protected_path" "$state_dir"

  set +e
  PATH="$STUB_BIN:$PATH" \
    LC_ALL=C \
    LANG=C \
    CURL_LOG="$CURL_LOG" \
    WRANGLER_LOG="$WRANGLER_LOG" \
    WRANGLER_CAPTURE="$WRANGLER_CAPTURE" \
    CF_STATE_DIR="$state_dir" \
    TASK18_CLOUDFLARE_ACCOUNT_ID="$TASK18_CLOUDFLARE_ACCOUNT_ID" \
    TASK18_CLOUDFLARE_ZONE_ID="$TASK18_CLOUDFLARE_ZONE_ID" \
    TASK18_CLOUDFLARE_TEAM_NAME="$TASK18_CLOUDFLARE_TEAM_NAME" \
    CLOUDFLARE_API_TOKEN=test-token \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    MIM_TASK18_EDGE_ORIGIN_URL="https://mim-control-plane-123456.asia-northeast3.run.app" \
    MIM_TASK18_APP_GATEWAY_ORIGIN_URL="https://mim-app-gateway-123456.asia-northeast3.run.app" \
    MIM_TASK18_EDGE_ORIGIN_HMAC_KEY_ID="control-current" \
    MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_KEY_ID="app-current" \
    MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET="$CONTROL_SECRET" \
    MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET="$APP_SECRET" \
    MIM_PROJECT_NUMBER=123456789012 \
    bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi
  task18_assert_contains "$output_path" "$expected_substring" "$case_name" || FAILURES=$((FAILURES + 1))
}

run_cleanup_probe() {
  local case_name=$1
  local expected_exit=$2
  local http_error_mode=$3
  local expected_substring=$4
  local probe_dir="$TMP_DIR/$case_name"
  local payload_file="$probe_dir/payload.json"
  local output_path="$probe_dir/out.txt"
  local probe_log="$probe_dir/curl.log"
  local probe_state_dir="$probe_dir/cf-state"
  local log_line config_file response_file http_code

  mkdir -p "$probe_dir"
  mkdir -p "$probe_state_dir"
  printf '%s\n' '{"name":"cleanup-probe","type":"CNAME","content":"probe.example","proxied":true}' >"$payload_file"
  : >"$probe_log"

  set +e
  PATH="$STUB_BIN:$PATH" \
    LC_ALL=C \
    LANG=C \
    CURL_LOG="$probe_log" \
    CLOUDFLARE_API_TOKEN=test-token \
    CF_HTTP_ERROR_MODE="$http_error_mode" \
    TASK18_CLOUDFLARE_ACCOUNT_ID="$TASK18_CLOUDFLARE_ACCOUNT_ID" \
    TASK18_CLOUDFLARE_ZONE_ID="$TASK18_CLOUDFLARE_ZONE_ID" \
    TASK18_CLOUDFLARE_TEAM_NAME="$TASK18_CLOUDFLARE_TEAM_NAME" \
    CF_STATE_DIR="$probe_state_dir" \
    bash -c '
      set -euo pipefail
      script_dir=$1
      payload_file=$2
      . "$script_dir/../release/task18_lib.sh"
      . "$script_dir/cloudflare_api.sh"
      mim_cf_api_call GET "/zones/$TASK18_CLOUDFLARE_ZONE_ID"
    ' _ "$SCRIPT_DIR" "$payload_file" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi
  task18_assert_contains "$output_path" "$expected_substring" "$case_name" || FAILURES=$((FAILURES + 1))

  log_line=$(cat "$probe_log")
  IFS=$'\t' read -r _ _ config_file response_file http_code <<<"$log_line"
  [[ -n "$config_file" && -n "$response_file" ]] || { printf 'FAIL %s: missing logged temp paths\n' "$case_name" >&2; FAILURES=$((FAILURES + 1)); return; }
  [[ ! -e "$config_file" ]] || { printf 'FAIL %s: curl config file still exists: %s\n' "$case_name" "$config_file" >&2; FAILURES=$((FAILURES + 1)); return; }
  [[ ! -e "$response_file" ]] || { printf 'FAIL %s: response file still exists: %s\n' "$case_name" "$response_file" >&2; FAILURES=$((FAILURES + 1)); return; }
  [[ -n "$http_code" ]] || { printf 'FAIL %s: missing logged http code\n' "$case_name" >&2; FAILURES=$((FAILURES + 1)); return; }
}

run_apply_case applies_ready_plan 0 "Applied reviewed plan."
task18_assert_contains "$CURL_LOG" $'POST\thttps://api.cloudflare.com/client/v4/accounts/' applies_ready_plan || FAILURES=$((FAILURES + 1))
task18_assert_contains "$CURL_LOG" $'POST\thttps://api.cloudflare.com/client/v4/zones/' applies_ready_plan || FAILURES=$((FAILURES + 1))
task18_assert_contains "$WRANGLER_LOG" "--secrets-file" applies_ready_plan || FAILURES=$((FAILURES + 1))
python3 - "$WRANGLER_CAPTURE" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
assert config["vars"]["MIM_CONTROL_PUBLIC_HOSTNAME"] == "mim.madup.app"
assert config["vars"]["MIM_APP_HOST_SUFFIX"] == "madup.app"
assert config["vars"]["MIM_CONTROL_ORIGIN"] == "https://mim-control-plane-123456.asia-northeast3.run.app"
assert config["vars"]["MIM_APP_GATEWAY_ORIGIN"] == "https://mim-app-gateway-123456.asia-northeast3.run.app"
assert config["vars"]["MIM_PROJECT_NUMBER"] == "123456789012"
assert config["vars"]["MIM_CONTROL_ORIGIN_HMAC_KEY_ID"] == "control-current"
assert config["vars"]["MIM_APP_GATEWAY_ORIGIN_HMAC_KEY_ID"] == "app-current"
assert config["secrets"]["required"] == ["MIM_CONTROL_ORIGIN_HMAC_SECRET", "MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET"]
PY
task18_assert_not_contains "$CURL_LOG" "$CONTROL_SECRET" applies_ready_plan || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$CURL_LOG" "$APP_SECRET" applies_ready_plan || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$WRANGLER_LOG" "$CONTROL_SECRET" applies_ready_plan || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$WRANGLER_LOG" "$APP_SECRET" applies_ready_plan || FAILURES=$((FAILURES + 1))

run_cleanup_probe cleanup_probe_success 0 "" '"success": true'
run_cleanup_probe cleanup_probe_error 1 error 'simulated api error'

task18_write_valid_config "$TMP_DIR/blocked.env"
task18_write_protected_file "$TMP_DIR/blocked.protected"
mkdir -p "$TMP_DIR/cf-state-blocked"
rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
PATH="$STUB_BIN:$PATH" LC_ALL=C LANG=C CURL_LOG="$CURL_LOG" WRANGLER_LOG="$WRANGLER_LOG" WRANGLER_CAPTURE="$WRANGLER_CAPTURE" CF_STATE_DIR="$TMP_DIR/cf-state-blocked" TASK18_CLOUDFLARE_ACCOUNT_ID="$TASK18_CLOUDFLARE_ACCOUNT_ID" TASK18_CLOUDFLARE_ZONE_ID="$TASK18_CLOUDFLARE_ZONE_ID" TASK18_CLOUDFLARE_TEAM_NAME="$TASK18_CLOUDFLARE_TEAM_NAME" CLOUDFLARE_API_TOKEN=test-token MIM_CONFIG_FILE="$TMP_DIR/blocked.env" MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/blocked.protected" MIM_TASK18_EDGE_ORIGIN_URL="https://mim-control-plane-123456.asia-northeast3.run.app" MIM_TASK18_APP_GATEWAY_ORIGIN_URL="https://mim-app-gateway-123456.asia-northeast3.run.app" MIM_TASK18_EDGE_ORIGIN_HMAC_KEY_ID="control-current" MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_KEY_ID="app-current" MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET="$CONTROL_SECRET" MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET="$APP_SECRET" MIM_PROJECT_NUMBER=123456789012 CF_CONTROL_APP_MODE=same_aud CF_WILDCARD_APP_MODE=same_aud bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >/dev/null
PATH="$STUB_BIN:$PATH" LC_ALL=C LANG=C CURL_LOG="$CURL_LOG" WRANGLER_LOG="$WRANGLER_LOG" WRANGLER_CAPTURE="$WRANGLER_CAPTURE" CF_STATE_DIR="$TMP_DIR/cf-state-blocked" TASK18_CLOUDFLARE_ACCOUNT_ID="$TASK18_CLOUDFLARE_ACCOUNT_ID" TASK18_CLOUDFLARE_ZONE_ID="$TASK18_CLOUDFLARE_ZONE_ID" TASK18_CLOUDFLARE_TEAM_NAME="$TASK18_CLOUDFLARE_TEAM_NAME" CLOUDFLARE_API_TOKEN=test-token MIM_CONFIG_FILE="$TMP_DIR/blocked.env" MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/blocked.protected" MIM_TASK18_EDGE_ORIGIN_URL="https://mim-control-plane-123456.asia-northeast3.run.app" MIM_TASK18_APP_GATEWAY_ORIGIN_URL="https://mim-app-gateway-123456.asia-northeast3.run.app" MIM_TASK18_EDGE_ORIGIN_HMAC_KEY_ID="control-current" MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_KEY_ID="app-current" MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET="$CONTROL_SECRET" MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET="$APP_SECRET" MIM_PROJECT_NUMBER=123456789012 bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/blocked.out" 2>&1 || true
task18_assert_contains "$TMP_DIR/blocked.out" "Reviewed plan contains blockers" blocked_plan || FAILURES=$((FAILURES + 1))

task18_write_valid_config "$TMP_DIR/drift.env"
task18_write_protected_file "$TMP_DIR/drift.protected"
rm -rf "$TMP_DIR/drift.cf-state"
make_plan "$TMP_DIR/drift.env" "$TMP_DIR/drift.protected" "$TMP_DIR/drift.cf-state"
python3 - <<'PY' "$TMP_DIR/drift.cf-state/routes.json"
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "mim.madup.app/*": {"id": "route-control-1", "pattern": "mim.madup.app/*", "script": "wrong-worker"}
}, sort_keys=True))
PY
set +e
PATH="$STUB_BIN:$PATH" LC_ALL=C LANG=C CURL_LOG="$CURL_LOG" WRANGLER_LOG="$WRANGLER_LOG" WRANGLER_CAPTURE="$WRANGLER_CAPTURE" CF_STATE_DIR="$TMP_DIR/drift.cf-state" TASK18_CLOUDFLARE_ACCOUNT_ID="$TASK18_CLOUDFLARE_ACCOUNT_ID" TASK18_CLOUDFLARE_ZONE_ID="$TASK18_CLOUDFLARE_ZONE_ID" TASK18_CLOUDFLARE_TEAM_NAME="$TASK18_CLOUDFLARE_TEAM_NAME" CLOUDFLARE_API_TOKEN=test-token MIM_CONFIG_FILE="$TMP_DIR/drift.env" MIM_PROTECTED_PROJECTS_FILE="$TMP_DIR/drift.protected" MIM_TASK18_EDGE_ORIGIN_URL="https://mim-control-plane-123456.asia-northeast3.run.app" MIM_TASK18_APP_GATEWAY_ORIGIN_URL="https://mim-app-gateway-123456.asia-northeast3.run.app" MIM_TASK18_EDGE_ORIGIN_HMAC_KEY_ID="control-current" MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_KEY_ID="app-current" MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET="$CONTROL_SECRET" MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET="$APP_SECRET" MIM_PROJECT_NUMBER=123456789012 bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_PATH" >"$TMP_DIR/drift.out" 2>&1
drift_exit=$?
set -e
[[ "$drift_exit" -ne 0 ]] || { printf 'FAIL rejects_drift: expected failure\n' >&2; FAILURES=$((FAILURES + 1)); }
task18_assert_contains "$TMP_DIR/drift.out" "Discovery drift detected" rejects_drift || FAILURES=$((FAILURES + 1))

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s edge apply assertions failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS test_apply.sh\n'
