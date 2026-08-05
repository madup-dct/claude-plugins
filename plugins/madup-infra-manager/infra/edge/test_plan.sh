#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
TEST_LIB="$SCRIPT_DIR/../release/test_task18_lib.sh"
. "$TEST_LIB"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"; rm -f "$SCRIPT_DIR"/.state/test-edge-plan*.json "$SCRIPT_DIR"/.state/test-edge-plan*.json.sha256 2>/dev/null || true' EXIT
mkdir -p "$SCRIPT_DIR/.state"

CURL_LOG="$TMP_DIR/curl.log"
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
    --config) config_file=$2; shift 2 ;;
    -H) shift 2 ;;
    --data-binary) shift 2 ;;
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
if [[ -n "${data_file:-}" ]]; then
  grep -F 'Content-Type: application/json' "$config_file" >/dev/null
fi

printf '%s\t%s\t%s\t%s\t%s\n' "$method" "$url" "$config_file" "$output_file" "$http_code" >> "${CURL_LOG:?}"
[[ -n "$header_file" ]] && : >"$header_file"

body=$(METHOD="$method" URL="$url" python3 - <<'PY'
import json
import math
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
other_idp_id = "google-idp-2"

def ok(result, result_info=None):
    payload = {"success": True, "errors": [], "messages": [], "result": result}
    if result_info is not None:
        payload["result_info"] = result_info
    return json.dumps(payload)

def app_result(app_id, domain, aud, allowed_idps, auto_redirect=True, oauth=True, app_launcher_visible=False, name=None, oauth_overrides=None):
    payload = {
        "id": app_id,
        "domain": domain,
        "aud": aud,
        "name": name or f"madup-infra-manager:{domain}",
        "type": "self_hosted",
        "allowed_idps": allowed_idps,
        "auto_redirect_to_identity": auto_redirect,
        "app_launcher_visible": app_launcher_visible,
        "session_duration": "168h",
        "allow_iframe": False,
        "skip_interstitial": False,
        "destinations": [],
        "policies": [],
        "created_at": "2026-08-05T00:00:00Z",
        "updated_at": "2026-08-05T00:00:00Z",
    }
    if oauth:
        payload["oauth_configuration"] = {
            "enabled": True,
            "dynamic_client_registration": {
                "enabled": True,
                "allow_any_on_localhost": True,
                "allow_any_on_loopback": True,
                "allowed_uris": [],
            },
            "grant": {
                "access_token_lifetime": "15m",
                "session_duration": "168h",
            },
        }
        if oauth_overrides:
            for key, value in oauth_overrides.items():
                if isinstance(value, dict) and isinstance(payload["oauth_configuration"].get(key), dict):
                    payload["oauth_configuration"][key].update(value)
                else:
                    payload["oauth_configuration"][key] = value
    return payload

def webhook_app_result():
    return {
        "id": webhook_app_id,
        "domain": webhook_hostname,
        "name": f"madup-infra-manager:{webhook_hostname}",
        "type": "self_hosted",
        "app_launcher_visible": False,
        "allow_iframe": False,
        "skip_interstitial": False,
        "destinations": [],
        "policies": [],
        "created_at": "2026-08-05T00:00:00Z",
        "updated_at": "2026-08-05T00:00:00Z",
    }

def extra_overlap_app():
    return {
        "id": "extra-overlap-app-1",
        "domain": "docs.madup.app",
        "name": "madup-infra-manager:docs.madup.app",
        "type": "self_hosted",
        "allowed_idps": [google_idp_id],
        "auto_redirect_to_identity": True,
        "app_launcher_visible": False,
    }

def unrelated_app():
    return {
        "id": "extra-unrelated-app-1",
        "domain": "example.org",
        "name": "example-org-app",
        "type": "self_hosted",
        "allowed_idps": [google_idp_id],
        "auto_redirect_to_identity": True,
        "app_launcher_visible": False,
    }

def policy_result(policy_id, idp_id, email, name="mim-users-allow"):
    return {
        "id": policy_id,
        "name": name,
        "decision": "allow",
        "include": [{"gsuite": {"email": email, "identity_provider_id": idp_id}}],
        "require": [],
        "exclude": [],
        "precedence": 1,
        "created_at": "2026-08-05T00:00:00Z",
        "updated_at": "2026-08-05T00:00:00Z",
    }

parsed = urlparse(os.environ["URL"])
path = parsed.path.split("/client/v4", 1)[1]
query = parse_qs(parsed.query)
method = os.environ["METHOD"]

control_mode = os.environ.get("CF_CONTROL_APP_MODE", "ready")
wildcard_mode = os.environ.get("CF_WILDCARD_APP_MODE", "ready")
webhook_mode = os.environ.get("CF_WEBHOOK_APP_MODE", "ready")
control_policy_mode = os.environ.get("CF_CONTROL_POLICY_MODE", "ready")
wildcard_policy_mode = os.environ.get("CF_WILDCARD_POLICY_MODE", "ready")
webhook_policy_mode = os.environ.get("CF_WEBHOOK_POLICY_MODE", "ready")
control_dns_mode = os.environ.get("CF_CONTROL_DNS_MODE", "ready")
wildcard_dns_mode = os.environ.get("CF_WILDCARD_DNS_MODE", "ready")
control_route_mode = os.environ.get("CF_CONTROL_ROUTE_MODE", "ready")
wildcard_route_mode = os.environ.get("CF_WILDCARD_ROUTE_MODE", "ready")
script_mode = os.environ.get("CF_SCRIPT_MODE", "ready")
secret_mode = os.environ.get("CF_SECRET_BINDING_MODE", "ready")
access_apps_total_pages = int(os.environ.get("CF_ACCESS_APPS_TOTAL_PAGES", "1"))
access_apps_metadata_mode = os.environ.get("CF_ACCESS_APPS_METADATA_MODE", "ready")
access_apps_page2_mode = os.environ.get("CF_ACCESS_APPS_PAGE2_MODE", "none")
access_apps_page2_total_pages = int(os.environ.get("CF_ACCESS_APPS_PAGE2_TOTAL_PAGES", str(access_apps_total_pages)))

def control_apps():
    if control_mode == "missing":
        return []
    payload = app_result(control_app_id, hostname, "control-audience-12345678", [google_idp_id], True, True)
    if control_mode == "same_aud":
        payload["aud"] = "shared-audience-12345678"
    elif control_mode == "wrong_idp":
        payload["allowed_idps"] = [other_idp_id]
    elif control_mode == "wrong_name":
        payload["name"] = "control-app"
    elif control_mode == "wrong_launcher":
        payload["app_launcher_visible"] = True
    elif control_mode == "wrong_oauth":
        payload["oauth_configuration"]["grant"]["session_duration"] = "1h"
    elif control_mode == "wrong_session_duration":
        payload["session_duration"] = "1h"
    result = [payload]
    if control_mode == "duplicate":
        result.append(app_result("control-app-dup", hostname, "control-audience-dup", [google_idp_id], True, True))
    elif control_mode == "extra_overlap":
        result.append(extra_overlap_app())
    elif control_mode == "unrelated":
        result.append(unrelated_app())
    return result

def wildcard_apps():
    if wildcard_mode == "missing":
        return []
    payload = app_result(wildcard_app_id, wildcard_hostname, "wildcard-audience-87654321", [google_idp_id], True, False, app_launcher_visible=False)
    if wildcard_mode == "same_aud":
        payload["aud"] = "shared-audience-12345678"
    elif wildcard_mode == "wrong_idp":
        payload["allowed_idps"] = [other_idp_id]
    elif wildcard_mode == "wrong_name":
        payload["name"] = "wildcard-app"
    elif wildcard_mode == "wrong_launcher":
        payload["app_launcher_visible"] = True
    elif wildcard_mode == "wrong_oauth":
        payload["oauth_configuration"] = {
            "enabled": True,
            "dynamic_client_registration": {
                "enabled": True,
                "allow_any_on_localhost": True,
                "allow_any_on_loopback": True,
                "allowed_uris": [],
            },
            "grant": {
                "access_token_lifetime": "15m",
                "session_duration": "168h",
            },
        }
    elif wildcard_mode == "wrong_session_duration":
        payload["session_duration"] = "1h"
    result = [payload]
    if wildcard_mode == "duplicate":
        result.append(app_result("wildcard-app-dup", wildcard_hostname, "wildcard-audience-dup", [google_idp_id], True, False, app_launcher_visible=False))
    elif wildcard_mode == "extra_overlap":
        result.append(extra_overlap_app())
    elif wildcard_mode == "unrelated":
        result.append(unrelated_app())
    return result

def webhook_apps():
    if webhook_mode == "missing":
        return []
    payload = webhook_app_result()
    if webhook_mode == "wrong_name":
        payload["name"] = "madup-infra-manager:wrong-webhook"
    elif webhook_mode == "wrong_launcher":
        payload["app_launcher_visible"] = True
    result = [payload]
    if webhook_mode == "duplicate":
        result.append({
            "id": "webhook-app-dup",
            "domain": webhook_hostname,
            "name": f"madup-infra-manager:{webhook_hostname}",
            "type": "self_hosted",
            "app_launcher_visible": False,
        })
    elif webhook_mode == "extra_overlap":
        result.append(extra_overlap_app())
    elif webhook_mode == "unrelated":
        result.append(unrelated_app())
    return result

def access_apps_for_page(page):
    if page == 1:
        return control_apps() + wildcard_apps() + webhook_apps()
    if page == 2:
        if access_apps_page2_mode == "error":
            raise SystemExit("simulated access apps page 2 failure")
        if access_apps_page2_mode == "duplicate_control":
            return [app_result("control-app-dup", hostname, "control-audience-dup", [google_idp_id], True, True)]
        if access_apps_page2_mode == "duplicate_wildcard":
            return [app_result("wildcard-app-dup", wildcard_hostname, "wildcard-audience-dup", [google_idp_id], True, False, app_launcher_visible=False)]
        if access_apps_page2_mode == "managed_overlap":
            return [extra_overlap_app()]
        if access_apps_page2_mode == "unrelated":
            return [unrelated_app()]
        return []
    return []

def access_apps_result(page):
    result = access_apps_for_page(page)
    if page == 1 and access_apps_metadata_mode == "missing":
        return json.dumps({"success": True, "errors": [], "messages": [], "result": result})
    total_pages = access_apps_total_pages if page == 1 else access_apps_page2_total_pages
    if page == 1 and access_apps_metadata_mode == "bad_total_pages":
        total_pages = "two"
    if page == 2 and access_apps_metadata_mode == "missing":
        return json.dumps({"success": True, "errors": [], "messages": [], "result": result})
    payload = {
        "success": True,
        "errors": [],
        "messages": [],
        "result": result,
        "result_info": {"total_pages": total_pages},
    }
    return json.dumps(payload)

if method == "GET" and path == f"/zones/{zone_id}":
    zone_name = os.environ.get("CF_ZONE_NAME", "madup.app")
    zone_account = os.environ.get("CF_ZONE_ACCOUNT_ID", account_id)
    zone_status = os.environ.get("CF_ZONE_STATUS", "active")
    zone_nameservers = os.environ.get("CF_ZONE_NAMESERVERS", "mina.ns.cloudflare.com,pete.ns.cloudflare.com").split(",")
    print(ok({"id": zone_id, "name": zone_name, "status": zone_status, "account": {"id": zone_account}, "name_servers": zone_nameservers}))
elif method == "GET" and path == f"/accounts/{account_id}/access/organizations":
    auth_domain = os.environ.get("CF_AUTH_DOMAIN", f"{team_name}.cloudflareaccess.com")
    print(ok({"auth_domain": auth_domain}))
elif method == "GET" and path == f"/accounts/{account_id}/access/identity_providers":
    idp_mode = os.environ.get("CF_GOOGLE_IDP_MODE", "single")
    if idp_mode == "missing":
        result = []
    elif idp_mode == "multiple":
        result = [{"id": google_idp_id, "type": "google-apps"}, {"id": other_idp_id, "type": "google-apps"}]
    else:
        result = [{"id": google_idp_id, "type": "google-apps"}]
    print(ok(result))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps":
    page = int(query.get("page", ["1"])[0])
    print(access_apps_result(page))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{control_app_id}":
    apps = control_apps()
    print(ok(apps[0] if apps else {}))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{wildcard_app_id}":
    apps = wildcard_apps()
    print(ok(apps[0] if apps else {}))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{webhook_app_id}":
    apps = webhook_apps()
    print(ok(apps[0] if apps else {}))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{control_app_id}/policies":
    if control_policy_mode == "missing":
        result = []
    elif control_policy_mode == "extra":
        result = [
            policy_result("control-policy-1", google_idp_id, allowed_group),
            policy_result("control-policy-2", google_idp_id, allowed_group),
        ]
    elif control_policy_mode == "wrong_group":
        result = [policy_result("control-policy-1", google_idp_id, "all@madup.com")]
    elif control_policy_mode == "wrong_shape":
        result = [{
            "id": "control-policy-1",
            "name": "mim-users-allow",
            "decision": "allow",
            "include": [{"everyone": {}}],
            "require": [],
            "exclude": [],
        }]
    elif control_policy_mode == "wrong_name":
        result = [policy_result("control-policy-1", google_idp_id, allowed_group, "wrong-control-policy")]
    else:
        result = [policy_result("control-policy-1", google_idp_id, allowed_group)]
    print(ok(result))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{wildcard_app_id}/policies":
    if wildcard_policy_mode == "missing":
        result = []
    elif wildcard_policy_mode == "extra":
        result = [
            policy_result("wildcard-policy-1", google_idp_id, allowed_group),
            policy_result("wildcard-policy-2", google_idp_id, allowed_group),
        ]
    elif wildcard_policy_mode == "wrong_group":
        result = [policy_result("wildcard-policy-1", google_idp_id, "all@madup.com")]
    elif wildcard_policy_mode == "wrong_shape":
        result = [{
            "id": "wildcard-policy-1",
            "name": "mim-users-allow",
            "decision": "allow",
            "include": [{"everyone": {}}],
            "require": [],
            "exclude": [],
        }]
    elif wildcard_policy_mode == "wrong_name":
        result = [policy_result("wildcard-policy-1", google_idp_id, allowed_group, "wrong-wildcard-policy")]
    else:
        result = [policy_result("wildcard-policy-1", google_idp_id, allowed_group)]
    print(ok(result))
elif method == "GET" and path == f"/accounts/{account_id}/access/apps/{webhook_app_id}/policies":
    if webhook_policy_mode == "missing":
        result = []
    elif webhook_policy_mode == "extra":
        result = [
            {"id": "webhook-policy-1", "name": "github-webhook-bypass", "decision": "bypass", "include": [{"everyone": {}}], "require": [], "exclude": [], "precedence": 1, "created_at": "2026-08-05T00:00:00Z", "updated_at": "2026-08-05T00:00:00Z"},
            {"id": "webhook-policy-2", "name": "github-webhook-bypass", "decision": "bypass", "include": [{"everyone": {}}], "require": [], "exclude": [], "precedence": 1, "created_at": "2026-08-05T00:00:00Z", "updated_at": "2026-08-05T00:00:00Z"},
        ]
    elif webhook_policy_mode == "wrong_shape":
        result = [{"id": "webhook-policy-1", "name": "github-webhook-bypass", "decision": "allow", "include": [{"everyone": {}}], "require": [], "exclude": [], "precedence": 1, "created_at": "2026-08-05T00:00:00Z", "updated_at": "2026-08-05T00:00:00Z"}]
    elif webhook_policy_mode == "wrong_name":
        result = [{"id": "webhook-policy-1", "name": "wrong-webhook-policy", "decision": "bypass", "include": [{"everyone": {}}], "require": [], "exclude": [], "precedence": 1, "created_at": "2026-08-05T00:00:00Z", "updated_at": "2026-08-05T00:00:00Z"}]
    else:
        result = [{"id": "webhook-policy-1", "name": "github-webhook-bypass", "decision": "bypass", "include": [{"everyone": {}}], "require": [], "exclude": [], "precedence": 1, "created_at": "2026-08-05T00:00:00Z", "updated_at": "2026-08-05T00:00:00Z"}]
    print(ok(result))
elif method == "GET" and path == f"/accounts/{account_id}/access/users":
    seat_count = int(os.environ.get("CF_ACCESS_SEAT_COUNT", "12"))
    page = int(query.get("page", ["1"])[0])
    per_page = int(query.get("per_page", ["100"])[0])
    total_pages = max(1, math.ceil(seat_count / per_page))
    start = (page - 1) * per_page
    end = min(seat_count, start + per_page)
    result = [{"id": f"user-{index}", "email": f"user-{index}@madup.com", "access_seat": True} for index in range(start, end)]
    print(ok(result, {"count": len(result), "page": page, "per_page": per_page, "total_count": seat_count, "total_pages": total_pages}))
elif method == "GET" and path == f"/zones/{zone_id}/dns_records":
    requested_name = query.get("name", [""])[0]
    if requested_name == hostname:
        mode = control_dns_mode
        target = control_target
        record_id = "dns-control-1"
    elif requested_name == wildcard_hostname:
        mode = wildcard_dns_mode
        target = wildcard_target
        record_id = "dns-wildcard-1"
    else:
        mode = "missing"
        target = ""
        record_id = "dns-none"
    if mode == "missing":
        result = []
    elif mode == "duplicate":
        result = [
            {"id": record_id, "type": "CNAME", "name": requested_name, "content": target, "proxied": True},
            {"id": f"{record_id}-2", "type": "CNAME", "name": requested_name, "content": target, "proxied": True},
        ]
    elif mode == "mismatch":
        result = [{"id": record_id, "type": "CNAME", "name": requested_name, "content": "wrong.run.app", "proxied": True}]
    else:
        result = [{"id": record_id, "type": "CNAME", "name": requested_name, "content": target, "proxied": True}]
    print(ok(result))
elif method == "GET" and path == f"/zones/{zone_id}/workers/routes":
    result = []
    if control_route_mode != "missing":
        result.append({
            "id": "route-control-1",
            "pattern": "mim.madup.app/*",
            "script": worker_name if control_route_mode == "ready" else "wrong-worker",
        })
    if wildcard_route_mode != "missing":
        result.append({
            "id": "route-wildcard-1",
            "pattern": "*.madup.app/*",
            "script": worker_name if wildcard_route_mode == "ready" else "wrong-worker",
        })
    shadow_mode = os.environ.get("CF_ROUTE_SHADOW_MODE", "none")
    if shadow_mode == "overlap-wildcard":
        result.append({"id": "route-shadow-1", "pattern": "blog.madup.app/*", "script": "other-worker"})
    elif shadow_mode == "overlap-control":
        result.append({"id": "route-shadow-1", "pattern": "mim.madup.app/api/*", "script": "other-worker"})
    elif shadow_mode == "non-overlap":
        result.append({"id": "route-other-1", "pattern": "example.org/*", "script": "other-worker"})
    print(ok(result))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts":
    print(ok([] if script_mode == "missing" else [{"id": worker_name}]))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts/{worker_name}/secrets":
    if secret_mode == "missing":
        result = []
    elif secret_mode == "extra":
        result = [
            {"name": "MIM_CONTROL_ORIGIN_HMAC_SECRET", "type": "secret_text"},
            {"name": "MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET", "type": "secret_text"},
            {"name": "MIM_UNUSED_SECRET", "type": "secret_text"},
        ]
    else:
        result = [
            {"name": "MIM_CONTROL_ORIGIN_HMAC_SECRET", "type": "secret_text"},
            {"name": "MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET", "type": "secret_text"},
        ]
    print(ok(result))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts/{worker_name}/secrets/MIM_CONTROL_ORIGIN_HMAC_SECRET":
    if secret_mode == "missing":
        print(ok({}))
    else:
        print(ok({"name": "MIM_CONTROL_ORIGIN_HMAC_SECRET", "type": "secret_text"}))
elif method == "GET" and path == f"/accounts/{account_id}/workers/scripts/{worker_name}/secrets/MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET":
    if secret_mode == "missing":
        print(ok({}))
    else:
        print(ok({"name": "MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET", "type": "secret_text"}))
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

FAILURES=0
PLAN_PATH="$SCRIPT_DIR/.state/test-edge-plan-ready.json"
EDGE_SECRET_VALUE=0123456789abcdef0123456789abcdef
APP_SECRET_VALUE=fedcba9876543210fedcba9876543210

run_case() {
  local case_name=$1
  local expected_exit=$2
  local expected_substring=$3
  shift 3
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"
  local output_path="$TMP_DIR/$case_name.out"
  task18_write_valid_config "$config_path"
  task18_write_protected_file "$protected_path"
  rm -f "$PLAN_PATH" "$PLAN_PATH.sha256"
  : >"$CURL_LOG"

  set +e
  PATH="$STUB_BIN:$PATH" \
    LC_ALL=C \
    LANG=C \
    CURL_LOG="$CURL_LOG" \
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
    MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET="$EDGE_SECRET_VALUE" \
    MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET="$APP_SECRET_VALUE" \
    MIM_PROJECT_NUMBER=123456789012 \
    env "$@" \
    bash "$PLAN_SCRIPT" --plan --out "$PLAN_PATH" >"$output_path" 2>&1
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
  local log_line config_file response_file http_code

  mkdir -p "$probe_dir"
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

run_token_rejection_case() {
  local case_name=$1
  local token_value=$2
  local output_path="$TMP_DIR/$case_name.out"
  : >"$CURL_LOG"

  set +e
  PATH="$STUB_BIN:$PATH" \
    LC_ALL=C \
    LANG=C \
    CURL_LOG="$CURL_LOG" \
    CLOUDFLARE_API_TOKEN="$token_value" \
    bash -c '
      set -euo pipefail
      script_dir=$1
      . "$script_dir/../release/task18_lib.sh"
      . "$script_dir/cloudflare_api.sh"
      mim_cf_api_call GET "/zones/test-zone"
    ' _ "$SCRIPT_DIR" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  [[ "$exit_code" -ne 0 ]] || { printf 'FAIL %s: expected token rejection\n' "$case_name" >&2; FAILURES=$((FAILURES + 1)); return; }
  task18_assert_contains "$output_path" "unsupported characters" "$case_name" || FAILURES=$((FAILURES + 1))
  [[ ! -s "$CURL_LOG" ]] || { printf 'FAIL %s: curl should not have been called\n' "$case_name" >&2; FAILURES=$((FAILURES + 1)); return; }
}

run_case ready 0 "Wrote reviewed plan"
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
if plan["status"] != "ready":
    raise RuntimeError(
        {
            "status": plan["status"],
            "blocker_codes": sorted(item["code"] for item in plan["blockers"]),
        }
    )
assert plan["targets"]["control_hostname"] == "mim.madup.app"
assert plan["targets"]["wildcard_hostname"] == "*.madup.app"
assert plan["targets"]["control_dns_target_hostname"] == "mim-control-plane-123456.asia-northeast3.run.app"
assert plan["targets"]["wildcard_dns_target_hostname"] == "mim-app-gateway-123456.asia-northeast3.run.app"
assert plan["targets"]["authoritative_nameservers"] == ["mina.ns.cloudflare.com", "pete.ns.cloudflare.com"]
assert plan["initial_state"]["control_access_app_status"] == "ready"
assert plan["initial_state"]["wildcard_access_app_status"] == "ready"
assert plan["initial_state"]["control_access_policy_status"] == "ready"
assert plan["initial_state"]["wildcard_access_policy_status"] == "ready"
assert plan["initial_state"]["github_webhook_access_app_status"] == "ready"
assert plan["initial_state"]["github_webhook_access_policy_status"] == "ready"
assert plan["initial_state"]["worker_secret_binding_status"] == "ready"
assert plan["initial_state"]["project_number_status"] == "ready"
assert plan["initial_state"]["control_access_audience"] != plan["initial_state"]["wildcard_access_audience"]
assert plan["initial_state"]["zone_status"] == "ready"
assert plan["initial_state"]["route_shadow_status"] == "ready"
assert plan["actions"] == []
PY

run_case creates_missing_resources 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=missing CF_WILDCARD_APP_MODE=missing CF_WEBHOOK_APP_MODE=missing CF_CONTROL_POLICY_MODE=missing CF_WILDCARD_POLICY_MODE=missing CF_WEBHOOK_POLICY_MODE=missing CF_CONTROL_DNS_MODE=missing CF_WILDCARD_DNS_MODE=missing CF_CONTROL_ROUTE_MODE=missing CF_WILDCARD_ROUTE_MODE=missing CF_SCRIPT_MODE=missing CF_SECRET_BINDING_MODE=missing
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
action_kinds = [action["kind"] for action in plan["actions"]]
assert action_kinds == [
    "upsert_access_app",
    "upsert_access_policy",
    "upsert_access_app",
    "upsert_access_policy",
    "upsert_access_app",
    "upsert_access_policy",
    "deploy_worker",
    "upsert_dns_record",
    "upsert_dns_record",
    "upsert_worker_route",
    "upsert_worker_route",
]
assert plan["actions"][0]["before_state"] == {"status": "missing"}
assert plan["actions"][1]["before_state"] == {"status": "missing"}
assert plan["actions"][6]["before_state"]["worker_status"] == "missing"
assert plan["actions"][6]["project_number"] == "123456789012"
assert plan["actions"][7]["before_state"] == {"status": "missing"}
assert plan["actions"][9]["before_state"] == {"status": "missing"}
PY

run_case blocks_same_audience 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=same_aud CF_WILDCARD_APP_MODE=same_aud
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["audience_distinct_status"] == "mismatch"
assert any(item["code"] == "access-app-audience-distinct-required" for item in plan["blockers"])
PY

run_case blocks_wrong_policy_shape 0 "Wrote reviewed plan" CF_WILDCARD_POLICY_MODE=wrong_shape
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["wildcard_access_policy_status"] == "mismatch"
assert any(item["code"] == "wildcard-access-policy-required" for item in plan["blockers"])
PY

run_case blocks_broad_webhook_bypass 0 "Wrote reviewed plan" CF_WEBHOOK_POLICY_MODE=extra
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["github_webhook_access_policy_status"] == "mismatch"
assert any(item["code"] == "github-webhook-bypass-policy-required" for item in plan["blockers"])
PY

run_case blocks_wrong_zone_status 0 "Wrote reviewed plan" CF_ZONE_STATUS=pending
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["zone_status"] == "mismatch"
assert any(item["code"] == "cloudflare-zone-mismatch" for item in plan["blockers"])
PY

run_case blocks_wrong_zone_nameservers 0 "Wrote reviewed plan" CF_ZONE_NAMESERVERS=pete.ns.cloudflare.com,wrong.ns.cloudflare.com
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["zone_nameservers_status"] == "mismatch"
assert any(item["code"] == "cloudflare-zone-nameservers-mismatch" for item in plan["blockers"])
PY

run_case blocks_wrong_zone_account 0 "Wrote reviewed plan" CF_ZONE_ACCOUNT_ID=other-account
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["zone_status"] == "mismatch"
assert any(item["code"] == "cloudflare-zone-mismatch" for item in plan["blockers"])
PY

run_case blocks_route_shadow 0 "Wrote reviewed plan" CF_ROUTE_SHADOW_MODE=overlap-wildcard
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["route_shadow_status"] == "mismatch"
assert any(item["code"] == "worker-route-shadow-detected" for item in plan["blockers"])
PY

run_case allows_non_overlapping_route 0 "Wrote reviewed plan" CF_ROUTE_SHADOW_MODE=non-overlap
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["initial_state"]["route_shadow_status"] == "ready"
PY

run_case allows_unrelated_account_app 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=unrelated
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["initial_state"]["managed_access_app_overlap_status"] == "ready"
PY

run_case allows_unrelated_page2_app 0 "Wrote reviewed plan" CF_ACCESS_APPS_TOTAL_PAGES=2 CF_ACCESS_APPS_PAGE2_MODE=unrelated
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "ready"
assert plan["initial_state"]["managed_access_app_overlap_status"] == "ready"
assert plan["initial_state"]["control_access_app_inventory_status"] == "ready"
PY

run_case blocks_page2_duplicate_control_app 0 "Wrote reviewed plan" CF_ACCESS_APPS_TOTAL_PAGES=2 CF_ACCESS_APPS_PAGE2_MODE=duplicate_control
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["control_access_app_inventory_status"] == "duplicate"
assert any(item["code"] == "control-access-app-duplicate-detected" for item in plan["blockers"])
PY

run_case blocks_page2_managed_overlap 0 "Wrote reviewed plan" CF_ACCESS_APPS_TOTAL_PAGES=2 CF_ACCESS_APPS_PAGE2_MODE=managed_overlap
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["managed_access_app_overlap_status"] == "mismatch"
assert any(item["code"] == "managed-access-app-overlap-detected" for item in plan["blockers"])
PY

run_case rejects_missing_access_apps_metadata 1 "Cloudflare Access apps page 1 is missing pagination metadata" CF_ACCESS_APPS_METADATA_MODE=missing
run_case rejects_bad_access_apps_metadata 1 "Cloudflare Access apps page 1 has invalid pagination metadata" CF_ACCESS_APPS_METADATA_MODE=bad_total_pages
run_case rejects_inconsistent_access_apps_metadata 1 "Cloudflare Access apps pagination metadata changed between pages" CF_ACCESS_APPS_TOTAL_PAGES=2 CF_ACCESS_APPS_PAGE2_MODE=unrelated CF_ACCESS_APPS_PAGE2_TOTAL_PAGES=3
run_case rejects_access_apps_page_fetch_failure 1 "simulated access apps page 2 failure" CF_ACCESS_APPS_TOTAL_PAGES=2 CF_ACCESS_APPS_PAGE2_MODE=error

run_case blocks_duplicate_control_app 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=duplicate
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["control_access_app_inventory_status"] == "duplicate"
assert any(item["code"] == "control-access-app-duplicate-detected" for item in plan["blockers"])
PY

run_case blocks_duplicate_wildcard_app 0 "Wrote reviewed plan" CF_WILDCARD_APP_MODE=duplicate
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["wildcard_access_app_inventory_status"] == "duplicate"
assert any(item["code"] == "wildcard-access-app-duplicate-detected" for item in plan["blockers"])
PY

run_case blocks_duplicate_webhook_app 0 "Wrote reviewed plan" CF_WEBHOOK_APP_MODE=duplicate
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["github_webhook_access_app_inventory_status"] == "duplicate"
assert any(item["code"] == "github-webhook-access-app-duplicate-detected" for item in plan["blockers"])
PY

run_case blocks_overlapping_extra_app 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=extra_overlap
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["managed_access_app_overlap_status"] == "mismatch"
assert any(item["code"] == "managed-access-app-overlap-detected" for item in plan["blockers"])
PY

run_case blocks_control_app_name_drift 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=wrong_name
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["control_access_app_status"] == "mismatch"
assert any(item["code"] == "control-access-app-required" for item in plan["blockers"])
PY

run_case blocks_wildcard_app_launcher_drift 0 "Wrote reviewed plan" CF_WILDCARD_APP_MODE=wrong_launcher
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["wildcard_access_app_status"] == "mismatch"
assert any(item["code"] == "wildcard-access-app-required" for item in plan["blockers"])
PY

run_case blocks_control_app_oauth_drift 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=wrong_oauth
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["control_access_app_status"] == "mismatch"
assert any(item["code"] == "control-access-app-required" for item in plan["blockers"])
PY

run_case blocks_control_app_session_duration_drift 0 "Wrote reviewed plan" CF_CONTROL_APP_MODE=wrong_session_duration
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["control_access_app_status"] == "mismatch"
assert any(item["code"] == "control-access-app-required" for item in plan["blockers"])
PY

run_case blocks_control_policy_name_drift 0 "Wrote reviewed plan" CF_CONTROL_POLICY_MODE=wrong_name
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["control_access_policy_status"] == "mismatch"
assert any(item["code"] == "control-access-policy-required" for item in plan["blockers"])
PY

run_case blocks_webhook_policy_name_drift 0 "Wrote reviewed plan" CF_WEBHOOK_POLICY_MODE=wrong_name
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["github_webhook_access_policy_status"] == "mismatch"
assert any(item["code"] == "github-webhook-bypass-policy-required" for item in plan["blockers"])
PY

run_case blocks_seat_overflow 0 "Wrote reviewed plan" CF_ACCESS_SEAT_COUNT=50
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "cloudflare-access-pilot-seat-limit-exceeded" for item in plan["blockers"])
PY

run_case blocks_extra_worker_secret 0 "Wrote reviewed plan" CF_SECRET_BINDING_MODE=extra
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["worker_secret_binding_status"] == "mismatch"
assert any(item["code"] == "worker-secret-binding-required" for item in plan["blockers"])
PY

run_case rejects_missing_app_gateway_origin 0 "Wrote reviewed plan" MIM_TASK18_APP_GATEWAY_ORIGIN_URL=
python3 - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "app-gateway-origin-url-required" for item in plan["blockers"])
PY

run_case rejects_invalid_project_number 0 "Wrote reviewed plan" MIM_PROJECT_NUMBER=not-a-number
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert plan["initial_state"]["project_number_status"] == "mismatch"
assert any(item["code"] == "project-number-required" for item in plan["blockers"])
PY

run_case rejects_newline_secret 0 "Wrote reviewed plan" MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET=$'line1\nline2________________________________'
python3 - "$PLAN_PATH" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
assert plan["status"] == "blocked"
assert any(item["code"] == "control-origin-secret-required" for item in plan["blockers"])
PY

task18_assert_not_contains "$PLAN_PATH" "$EDGE_SECRET_VALUE" no_secret_leak || FAILURES=$((FAILURES + 1))
task18_assert_not_contains "$PLAN_PATH" "$APP_SECRET_VALUE" no_secret_leak || FAILURES=$((FAILURES + 1))

run_cleanup_probe cleanup_probe_success 0 "" '"success": true'
run_cleanup_probe cleanup_probe_error 1 error 'simulated api error'
run_token_rejection_case token_reject_quote $'bad"token'
run_token_rejection_case token_reject_backslash $'bad\\token'
run_token_rejection_case token_reject_cr $'bad\rtoken'
run_token_rejection_case token_reject_lf $'bad\ntoken'
run_token_rejection_case token_reject_ctrl $'bad\x01token'

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s edge plan assertions failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS test_plan.sh\n'
