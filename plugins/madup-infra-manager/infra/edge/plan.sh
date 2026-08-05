#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"
. "$SCRIPT_DIR/cloudflare_api.sh"

CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"
MODE=
PLAN_OUT=
readonly MIM_EDGE_WORKER_NAME=madup-infra-manager-edge-worker
readonly MIM_EDGE_CONTROL_HOSTNAME="$MIM_TASK18_HOSTNAME"
readonly MIM_EDGE_WILDCARD_HOSTNAME="*.$MIM_TASK18_APP_HOST_SUFFIX"
readonly MIM_EDGE_GITHUB_WEBHOOK_HOSTNAME="${MIM_TASK18_GITHUB_WEBHOOK_URL#https://}"
readonly MIM_EDGE_CONTROL_ROUTE_PATTERN="$MIM_TASK18_HOSTNAME/*"
readonly MIM_EDGE_WILDCARD_ROUTE_PATTERN="*.$MIM_TASK18_APP_HOST_SUFFIX/*"
readonly MIM_EDGE_CONTROL_SECRET_NAME=MIM_CONTROL_ORIGIN_HMAC_SECRET
readonly MIM_EDGE_APP_SECRET_NAME=MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET
readonly MIM_EDGE_MANAGED_OAUTH_ACCESS_TOKEN_LIFETIME=15m
readonly MIM_EDGE_MANAGED_OAUTH_SESSION_DURATION=168h
readonly MIM_EDGE_PROJECT_NUMBER_PATTERN='^[1-9][0-9]{11}$'

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

validate_control_origin_url() {
  local value=${MIM_TASK18_EDGE_ORIGIN_URL:-}
  [[ -n "$value" ]] || return 1
  python3 - "$value" <<'PY' >/dev/null
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
if url.scheme != "https":
    raise SystemExit(1)
if not url.hostname or not url.hostname.endswith(".run.app"):
    raise SystemExit(1)
if url.path not in ("", "/") or url.params or url.query or url.fragment or url.username or url.password or url.port:
    raise SystemExit(1)
PY
}

validate_app_gateway_origin_url() {
  local value=${MIM_TASK18_APP_GATEWAY_ORIGIN_URL:-}
  [[ -n "$value" ]] || return 1
  python3 - "$value" <<'PY' >/dev/null
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
if url.scheme != "https":
    raise SystemExit(1)
hostname = url.hostname or ""
parts = hostname.split(".")
if len(parts) != 4:
    raise SystemExit(1)
service = parts[0]
if parts[1:] != ["asia-northeast3", "run", "app"]:
    raise SystemExit(1)
prefix = "mim-app-gateway-"
if not service.startswith(prefix) or not service[len(prefix):].isdigit():
    raise SystemExit(1)
if url.path not in ("", "/") or url.params or url.query or url.fragment or url.username or url.password or url.port:
    raise SystemExit(1)
PY
}

hostname_from_url() {
  local value=$1
  python3 - "$value" <<'PY'
import sys
from urllib.parse import urlparse
print(urlparse(sys.argv[1]).hostname or "")
PY
}

validate_key_id() {
  local value=$1
  [[ "$value" =~ ^[A-Za-z0-9._-]{1,128}$ ]]
}

validate_project_number() {
  local value=$1
  [[ "$value" =~ $MIM_EDGE_PROJECT_NUMBER_PATTERN ]]
}

validate_secret_value() {
  local value=$1
  [[ -n "$value" ]] || return 1
MIM_SECRET_CANDIDATE="$value" python3 - <<'PY' >/dev/null
import os
import sys
value = os.environ["MIM_SECRET_CANDIDATE"].encode("utf-8")
if b"\x00" in value or b"\r" in value or b"\n" in value:
    raise SystemExit(1)
if len(value) < 32:
    raise SystemExit(1)
PY
}

SNAPSHOT_DIR=$(mktemp -d)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$SNAPSHOT_DIR" "$TMP_DIR"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v wrangler >/dev/null 2>&1 || mim_task18_fail "wrangler CLI is required"

CONTROL_ORIGIN_URL="${MIM_TASK18_EDGE_ORIGIN_URL:-}"
APP_GATEWAY_ORIGIN_URL="${MIM_TASK18_APP_GATEWAY_ORIGIN_URL:-}"
CONTROL_DNS_TARGET=
APP_DNS_TARGET=
if validate_control_origin_url; then
  CONTROL_DNS_TARGET=$(hostname_from_url "$CONTROL_ORIGIN_URL")
fi
if validate_app_gateway_origin_url; then
  APP_DNS_TARGET=$(hostname_from_url "$APP_GATEWAY_ORIGIN_URL")
fi

CONTROL_KEY_ID="${MIM_TASK18_EDGE_ORIGIN_HMAC_KEY_ID:-}"
APP_KEY_ID="${MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_KEY_ID:-}"
CONTROL_SECRET_VALUE="${MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET:-}"
APP_SECRET_VALUE="${MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET:-}"
PROJECT_NUMBER="${MIM_PROJECT_NUMBER:-${MIM_TASK18_PROJECT_NUMBER:-}}"

ZONE_JSON="$TMP_DIR/zone.json"
ORG_JSON="$TMP_DIR/org.json"
IDP_JSON="$TMP_DIR/idps.json"
APPS_JSON="$TMP_DIR/apps.json"
CONTROL_APP_JSON="$TMP_DIR/control-app.json"
WILDCARD_APP_JSON="$TMP_DIR/wildcard-app.json"
WEBHOOK_APP_JSON="$TMP_DIR/webhook-app.json"
CONTROL_POLICIES_JSON="$TMP_DIR/control-policies.json"
WILDCARD_POLICIES_JSON="$TMP_DIR/wildcard-policies.json"
WEBHOOK_POLICIES_JSON="$TMP_DIR/webhook-policies.json"
CONTROL_DNS_JSON="$TMP_DIR/control-dns.json"
WILDCARD_DNS_JSON="$TMP_DIR/wildcard-dns.json"
ROUTES_JSON="$TMP_DIR/routes.json"
SCRIPTS_JSON="$TMP_DIR/scripts.json"
SECRETS_JSON="$TMP_DIR/secrets.json"
SEAT_COUNT_FILE="$TMP_DIR/access-seat-count.txt"

mim_cf_get_zone_json >"$ZONE_JSON"
mim_cf_get_organization_json >"$ORG_JSON"
mim_cf_list_identity_providers_json >"$IDP_JSON"
mim_cf_list_access_apps_json >"$APPS_JSON"
mim_cf_list_dns_records_json "$MIM_EDGE_CONTROL_HOSTNAME" >"$CONTROL_DNS_JSON"
mim_cf_list_dns_records_json "$MIM_EDGE_WILDCARD_HOSTNAME" >"$WILDCARD_DNS_JSON"
mim_cf_list_worker_routes_json >"$ROUTES_JSON"
mim_cf_list_worker_scripts_json >"$SCRIPTS_JSON"
ACCESS_SEAT_COUNT=$(mim_cf_count_access_seats)
printf '%s\n' "$ACCESS_SEAT_COUNT" >"$SEAT_COUNT_FILE"

python3 - "$IDP_JSON" "$APPS_JSON" "$TMP_DIR" "$MIM_EDGE_CONTROL_HOSTNAME" "$MIM_EDGE_WILDCARD_HOSTNAME" "$MIM_EDGE_GITHUB_WEBHOOK_HOSTNAME" <<'PY'
import json
import sys
from pathlib import Path

idps = json.loads(Path(sys.argv[1]).read_text())["result"]
apps = json.loads(Path(sys.argv[2]).read_text())["result"]
tmp_dir = Path(sys.argv[3])
control_domain = sys.argv[4].lower()
wildcard_domain = sys.argv[5].lower()
webhook_domain = sys.argv[6].lower()
managed_domains = {control_domain, wildcard_domain, webhook_domain}

def normalized_domain(app):
    return (app.get("domain") or "").lower()

def normalized_name(app):
    return (app.get("name") or "").lower()

def exact_matches(domain):
    return [app for app in apps if normalized_domain(app) == domain]

def write_text(name, value):
    (tmp_dir / name).write_text(f"{value}\n")

def write_selection(prefix, domain):
    matches = exact_matches(domain)
    if len(matches) == 1:
        write_text(f"{prefix}-app-status.txt", "ready")
        write_text(f"{prefix}-app-id.txt", str(matches[0].get("id", "")))
    elif not matches:
        write_text(f"{prefix}-app-status.txt", "missing")
    else:
        write_text(f"{prefix}-app-status.txt", "duplicate")

def overlaps_managed_namespace(app):
    domain = normalized_domain(app)
    host = domain.split("/", 1)[0]
    if normalized_name(app).startswith("madup-infra-manager:"):
        return True
    return host == "madup.app" or host.endswith(".madup.app")

google_idps = [item for item in idps if item.get("type") == "google-apps" and item.get("id")]
Path(tmp_dir / "google-idp-count.txt").write_text(str(len(google_idps)))
if len(google_idps) == 1:
    Path(tmp_dir / "google-idp-id.txt").write_text(str(google_idps[0]["id"]))

write_selection("control", control_domain)
write_selection("wildcard", wildcard_domain)
write_selection("webhook", webhook_domain)

managed_overlap_apps = []
for app in apps:
    domain = normalized_domain(app)
    if domain in managed_domains:
        continue
    if overlaps_managed_namespace(app):
        managed_overlap_apps.append({
            "id": app.get("id"),
            "name": app.get("name"),
            "domain": app.get("domain"),
        })

(tmp_dir / "managed-overlap-apps.json").write_text(json.dumps(managed_overlap_apps, sort_keys=True) + "\n")
(tmp_dir / "managed-overlap-status.txt").write_text(("mismatch" if managed_overlap_apps else "ready") + "\n")
PY

CONTROL_APP_ID=
WILDCARD_APP_ID=
WEBHOOK_APP_ID=
CONTROL_APP_INVENTORY_STATUS=missing
WILDCARD_APP_INVENTORY_STATUS=missing
WEBHOOK_APP_INVENTORY_STATUS=missing
MANAGED_ACCESS_APP_OVERLAP_STATUS=ready
if [[ -f "$TMP_DIR/control-app-status.txt" ]]; then
  CONTROL_APP_INVENTORY_STATUS=$(tr -d '\n' <"$TMP_DIR/control-app-status.txt")
fi
if [[ -f "$TMP_DIR/wildcard-app-status.txt" ]]; then
  WILDCARD_APP_INVENTORY_STATUS=$(tr -d '\n' <"$TMP_DIR/wildcard-app-status.txt")
fi
if [[ -f "$TMP_DIR/webhook-app-status.txt" ]]; then
  WEBHOOK_APP_INVENTORY_STATUS=$(tr -d '\n' <"$TMP_DIR/webhook-app-status.txt")
fi
if [[ -f "$TMP_DIR/managed-overlap-status.txt" ]]; then
  MANAGED_ACCESS_APP_OVERLAP_STATUS=$(tr -d '\n' <"$TMP_DIR/managed-overlap-status.txt")
fi
if [[ "$CONTROL_APP_INVENTORY_STATUS" == "ready" && -f "$TMP_DIR/control-app-id.txt" ]]; then
  CONTROL_APP_ID=$(tr -d '\n' <"$TMP_DIR/control-app-id.txt")
  [[ -n "$CONTROL_APP_ID" ]] && mim_cf_get_access_app_json "$CONTROL_APP_ID" >"$CONTROL_APP_JSON"
  [[ -n "$CONTROL_APP_ID" ]] && mim_cf_list_access_policies_json "$CONTROL_APP_ID" >"$CONTROL_POLICIES_JSON"
fi
if [[ "$WILDCARD_APP_INVENTORY_STATUS" == "ready" && -f "$TMP_DIR/wildcard-app-id.txt" ]]; then
  WILDCARD_APP_ID=$(tr -d '\n' <"$TMP_DIR/wildcard-app-id.txt")
  [[ -n "$WILDCARD_APP_ID" ]] && mim_cf_get_access_app_json "$WILDCARD_APP_ID" >"$WILDCARD_APP_JSON"
  [[ -n "$WILDCARD_APP_ID" ]] && mim_cf_list_access_policies_json "$WILDCARD_APP_ID" >"$WILDCARD_POLICIES_JSON"
fi
if [[ "$WEBHOOK_APP_INVENTORY_STATUS" == "ready" && -f "$TMP_DIR/webhook-app-id.txt" ]]; then
  WEBHOOK_APP_ID=$(tr -d '\n' <"$TMP_DIR/webhook-app-id.txt")
  [[ -n "$WEBHOOK_APP_ID" ]] && mim_cf_get_access_app_json "$WEBHOOK_APP_ID" >"$WEBHOOK_APP_JSON"
  [[ -n "$WEBHOOK_APP_ID" ]] && mim_cf_list_access_policies_json "$WEBHOOK_APP_ID" >"$WEBHOOK_POLICIES_JSON"
fi

if python3 - "$SCRIPTS_JSON" "$MIM_EDGE_WORKER_NAME" <<'PY' >/dev/null
import json
import sys
scripts = json.loads(open(sys.argv[1]).read())["result"]
name = sys.argv[2]
raise SystemExit(0 if any(item.get("id") == name for item in scripts) else 1)
PY
then
  mim_cf_list_worker_secrets_json "$MIM_EDGE_WORKER_NAME" >"$SECRETS_JSON"
fi

PLAN_PATH="$TMP_DIR/plan.json"
PLAN_GENERATED_AT="${MIM_TASK18_PLAN_GENERATED_AT:-$(mim_task18_now_epoch)}"
PLAN_EXPIRES_AT="${MIM_TASK18_PLAN_EXPIRES_AT:-$((PLAN_GENERATED_AT + MIM_TASK18_PLAN_MAX_AGE_SECONDS))}"
PLAN_CONFIG_FINGERPRINT=$(mim_task18_config_fingerprint "$SNAPSHOT_CONFIG")
PLAN_CONTROL_ORIGIN_VALID=false
PLAN_APP_ORIGIN_VALID=false
PLAN_DISTINCT_ORIGIN_VALID=false
PLAN_CONTROL_KEY_VALID=false
PLAN_APP_KEY_VALID=false
PLAN_CONTROL_SECRET_VALID=false
PLAN_APP_SECRET_VALID=false
PLAN_PROJECT_NUMBER_VALID=false

validate_control_origin_url && PLAN_CONTROL_ORIGIN_VALID=true
validate_app_gateway_origin_url && PLAN_APP_ORIGIN_VALID=true
if [[ "$PLAN_CONTROL_ORIGIN_VALID" == true && "$PLAN_APP_ORIGIN_VALID" == true && "$CONTROL_ORIGIN_URL" != "$APP_GATEWAY_ORIGIN_URL" ]]; then
  PLAN_DISTINCT_ORIGIN_VALID=true
fi
validate_key_id "$CONTROL_KEY_ID" && PLAN_CONTROL_KEY_VALID=true
validate_key_id "$APP_KEY_ID" && PLAN_APP_KEY_VALID=true
validate_secret_value "$CONTROL_SECRET_VALUE" && PLAN_CONTROL_SECRET_VALID=true
validate_secret_value "$APP_SECRET_VALUE" && PLAN_APP_SECRET_VALID=true
validate_project_number "$PROJECT_NUMBER" && PLAN_PROJECT_NUMBER_VALID=true

PLAN_ZONE_JSON="$ZONE_JSON" \
PLAN_ORG_JSON="$ORG_JSON" \
PLAN_IDP_JSON="$IDP_JSON" \
PLAN_APPS_JSON="$APPS_JSON" \
PLAN_CONTROL_APP_JSON="$CONTROL_APP_JSON" \
PLAN_WILDCARD_APP_JSON="$WILDCARD_APP_JSON" \
PLAN_WEBHOOK_APP_JSON="$WEBHOOK_APP_JSON" \
PLAN_CONTROL_POLICIES_JSON="$CONTROL_POLICIES_JSON" \
PLAN_WILDCARD_POLICIES_JSON="$WILDCARD_POLICIES_JSON" \
PLAN_WEBHOOK_POLICIES_JSON="$WEBHOOK_POLICIES_JSON" \
PLAN_CONTROL_DNS_JSON="$CONTROL_DNS_JSON" \
PLAN_WILDCARD_DNS_JSON="$WILDCARD_DNS_JSON" \
PLAN_ROUTES_JSON="$ROUTES_JSON" \
PLAN_SCRIPTS_JSON="$SCRIPTS_JSON" \
PLAN_SECRETS_JSON="$SECRETS_JSON" \
PLAN_CONTROL_APP_INVENTORY_STATUS="$CONTROL_APP_INVENTORY_STATUS" \
PLAN_WILDCARD_APP_INVENTORY_STATUS="$WILDCARD_APP_INVENTORY_STATUS" \
PLAN_WEBHOOK_APP_INVENTORY_STATUS="$WEBHOOK_APP_INVENTORY_STATUS" \
PLAN_MANAGED_ACCESS_APP_OVERLAP_STATUS="$MANAGED_ACCESS_APP_OVERLAP_STATUS" \
PLAN_MANAGED_ACCESS_APP_OVERLAP_JSON="$TMP_DIR/managed-overlap-apps.json" \
PLAN_ACCESS_SEAT_COUNT_FILE="$SEAT_COUNT_FILE" \
PLAN_ACCOUNT_ID="$MIM_CLOUDFLARE_ACCOUNT_ID" \
PLAN_ZONE_NAME="$MIM_TASK18_ZONE_NAME" \
PLAN_TEAM_NAME="$MIM_CLOUDFLARE_TEAM_NAME" \
PLAN_ZONE_NAMESERVERS="$MIM_TASK18_ZONE_AUTHORITATIVE_NAMESERVERS" \
PLAN_ALLOWED_GROUP_EMAIL="$MIM_TASK18_ALLOWED_GROUP_EMAIL" \
PLAN_CONTROL_HOSTNAME="$MIM_EDGE_CONTROL_HOSTNAME" \
PLAN_WILDCARD_HOSTNAME="$MIM_EDGE_WILDCARD_HOSTNAME" \
PLAN_GITHUB_WEBHOOK_HOSTNAME="$MIM_EDGE_GITHUB_WEBHOOK_HOSTNAME" \
PLAN_CONTROL_ROUTE_PATTERN="$MIM_EDGE_CONTROL_ROUTE_PATTERN" \
PLAN_WILDCARD_ROUTE_PATTERN="$MIM_EDGE_WILDCARD_ROUTE_PATTERN" \
PLAN_APP_HOST_SUFFIX="$MIM_TASK18_APP_HOST_SUFFIX" \
PLAN_CONTROL_ORIGIN_URL="$CONTROL_ORIGIN_URL" \
PLAN_APP_GATEWAY_ORIGIN_URL="$APP_GATEWAY_ORIGIN_URL" \
PLAN_CONTROL_DNS_TARGET="$CONTROL_DNS_TARGET" \
PLAN_APP_DNS_TARGET="$APP_DNS_TARGET" \
PLAN_CONTROL_KEY_ID="$CONTROL_KEY_ID" \
PLAN_APP_KEY_ID="$APP_KEY_ID" \
PLAN_PROJECT_NUMBER="$PROJECT_NUMBER" \
PLAN_CONTROL_SECRET_NAME="$MIM_EDGE_CONTROL_SECRET_NAME" \
PLAN_APP_SECRET_NAME="$MIM_EDGE_APP_SECRET_NAME" \
PLAN_CONTROL_ORIGIN_VALID="$PLAN_CONTROL_ORIGIN_VALID" \
PLAN_APP_ORIGIN_VALID="$PLAN_APP_ORIGIN_VALID" \
PLAN_DISTINCT_ORIGIN_VALID="$PLAN_DISTINCT_ORIGIN_VALID" \
PLAN_CONTROL_KEY_VALID="$PLAN_CONTROL_KEY_VALID" \
PLAN_APP_KEY_VALID="$PLAN_APP_KEY_VALID" \
PLAN_CONTROL_SECRET_VALID="$PLAN_CONTROL_SECRET_VALID" \
PLAN_APP_SECRET_VALID="$PLAN_APP_SECRET_VALID" \
PLAN_PROJECT_NUMBER_VALID="$PLAN_PROJECT_NUMBER_VALID" \
PLAN_MANAGED_OAUTH_ACCESS_TOKEN_LIFETIME="$MIM_EDGE_MANAGED_OAUTH_ACCESS_TOKEN_LIFETIME" \
PLAN_MANAGED_OAUTH_SESSION_DURATION="$MIM_EDGE_MANAGED_OAUTH_SESSION_DURATION" \
PLAN_GENERATED_AT="$PLAN_GENERATED_AT" \
PLAN_EXPIRES_AT="$PLAN_EXPIRES_AT" \
PLAN_OPERATOR_EMAIL="$MIM_OPERATOR_EMAIL" \
PLAN_PROJECT_ID="$MIM_PROJECT_ID" \
PLAN_ORGANIZATION_ID="$MIM_ORGANIZATION_ID" \
PLAN_BILLING_ACCOUNT_ID="$MIM_BILLING_ACCOUNT_ID" \
PLAN_CONFIG_FINGERPRINT="$PLAN_CONFIG_FINGERPRINT" \
python3 - "$PLAN_PATH" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

def read_json(path_env: str):
    path = os.environ.get(path_env, "")
    if not path:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return None
    return json.loads(candidate.read_text())["result"]

def normalize_string_list(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]

def normalize_nameservers(value):
    return sorted(item.lower().rstrip(".") for item in normalize_string_list(value))

def hash_compact(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def app_payload(domain: str, allowed_idps: list[str], oauth_enabled: bool):
    payload = {
        "name": f"madup-infra-manager:{domain}",
        "domain": domain,
        "type": "self_hosted",
        "allowed_idps": allowed_idps,
        "session_duration": os.environ["PLAN_MANAGED_OAUTH_SESSION_DURATION"],
        "auto_redirect_to_identity": True,
        "app_launcher_visible": False,
    }
    if oauth_enabled:
        payload["oauth_configuration"] = {
            "enabled": True,
            "dynamic_client_registration": {
                "enabled": True,
                "allow_any_on_localhost": True,
                "allow_any_on_loopback": True,
                "allowed_uris": [],
            },
            "grant": {
                "access_token_lifetime": os.environ["PLAN_MANAGED_OAUTH_ACCESS_TOKEN_LIFETIME"],
                "session_duration": os.environ["PLAN_MANAGED_OAUTH_SESSION_DURATION"],
            },
        }
    return payload

def policy_payload(idp_id: str):
    return {
        "name": "mim-users-allow",
        "decision": "allow",
        "include": [{
            "gsuite": {
                "email": os.environ["PLAN_ALLOWED_GROUP_EMAIL"],
                "identity_provider_id": idp_id,
            }
        }],
        "require": [],
        "exclude": [],
    }

def webhook_app_payload(domain: str):
    return {
        "name": f"madup-infra-manager:{domain}",
        "domain": domain,
        "type": "self_hosted",
        "app_launcher_visible": False,
    }

def webhook_policy_payload():
    return {
        "name": "github-webhook-bypass",
        "decision": "bypass",
        "include": [{"everyone": {}}],
        "require": [],
        "exclude": [],
    }

def project_control_app(payload):
    if not isinstance(payload, dict):
        return None
    projected = {key: payload.get(key) for key in ("name", "domain", "type", "allowed_idps", "session_duration", "auto_redirect_to_identity", "app_launcher_visible")}
    oauth = payload.get("oauth_configuration")
    if not isinstance(oauth, dict):
        return None
    dynamic = oauth.get("dynamic_client_registration")
    grant = oauth.get("grant")
    if not isinstance(dynamic, dict) or not isinstance(grant, dict):
        return None
    projected["oauth_configuration"] = {
        "enabled": oauth.get("enabled"),
        "dynamic_client_registration": {
            "enabled": dynamic.get("enabled"),
            "allow_any_on_localhost": dynamic.get("allow_any_on_localhost"),
            "allow_any_on_loopback": dynamic.get("allow_any_on_loopback"),
            "allowed_uris": dynamic.get("allowed_uris"),
        },
        "grant": {
            "access_token_lifetime": grant.get("access_token_lifetime"),
            "session_duration": grant.get("session_duration"),
        },
    }
    return projected

def project_disabled_access_app(payload, reviewed_keys):
    if not isinstance(payload, dict):
        return None
    projected = {key: payload.get(key) for key in reviewed_keys}
    oauth = payload.get("oauth_configuration")
    if oauth is None:
        return projected
    if isinstance(oauth, dict) and oauth.get("enabled") is False:
        return projected
    return None

def project_wildcard_app(payload):
    return project_disabled_access_app(payload, ("name", "domain", "type", "allowed_idps", "session_duration", "auto_redirect_to_identity", "app_launcher_visible"))

def project_webhook_app(payload):
    return project_disabled_access_app(payload, ("name", "domain", "type", "app_launcher_visible"))

def project_access_policy(payload):
    if not isinstance(payload, dict):
        return None
    return {key: payload.get(key) for key in ("name", "decision", "include", "require", "exclude")}

def exact_payload_status(payload, expected, projector):
    projected = projector(payload)
    if projected is None:
        return False
    return projected == expected

def exact_policy_status(policies, idp_id):
    if not policies:
        return "missing"
    if len(policies) != 1:
        return "mismatch"
    policy = policies[0]
    if not exact_payload_status(policy, policy_payload(idp_id), project_access_policy):
        return "mismatch"
    return "ready"

def exact_app_status(app, expected, projector):
    if app is None:
        return "missing"
    return "ready" if exact_payload_status(app, expected, projector) else "mismatch"

def exact_dns_status(records, hostname, target):
    if not records:
        return "missing"
    if len(records) != 1:
        return "ambiguous"
    record = records[0]
    if (
        record.get("type") == "CNAME"
        and record.get("name") == hostname
        and record.get("content") == target
        and record.get("proxied") is True
    ):
        return "ready"
    return "mismatch"

def exact_route_status(routes, pattern, worker_name):
    matches = [route for route in routes if route.get("pattern") == pattern]
    if not matches:
        return "missing"
    if len(matches) != 1:
        return "ambiguous"
    return "ready" if matches[0].get("script") == worker_name else "mismatch"

def overlaps_managed_route(pattern):
    if not isinstance(pattern, str) or "/" not in pattern:
        return False
    host = pattern.split("/", 1)[0].lower()
    if host == control_domain:
        return True
    if host == wildcard_domain:
        return True
    if host.endswith(".madup.app"):
        return True
    if "*" in host and host.endswith("madup.app"):
        return True
    return False

zone = read_json("PLAN_ZONE_JSON")
org = read_json("PLAN_ORG_JSON")
idps = read_json("PLAN_IDP_JSON") or []
apps = read_json("PLAN_APPS_JSON") or []
control_app = read_json("PLAN_CONTROL_APP_JSON")
wildcard_app = read_json("PLAN_WILDCARD_APP_JSON")
webhook_app = read_json("PLAN_WEBHOOK_APP_JSON")
control_policies = read_json("PLAN_CONTROL_POLICIES_JSON") or []
wildcard_policies = read_json("PLAN_WILDCARD_POLICIES_JSON") or []
webhook_policies = read_json("PLAN_WEBHOOK_POLICIES_JSON") or []
control_dns_records = read_json("PLAN_CONTROL_DNS_JSON") or []
wildcard_dns_records = read_json("PLAN_WILDCARD_DNS_JSON") or []
routes = read_json("PLAN_ROUTES_JSON") or []
scripts = read_json("PLAN_SCRIPTS_JSON") or []
secrets = read_json("PLAN_SECRETS_JSON") or []
seat_count = int(Path(os.environ["PLAN_ACCESS_SEAT_COUNT_FILE"]).read_text().strip())

expected_auth_domain = f'{os.environ["PLAN_TEAM_NAME"]}.cloudflareaccess.com'
expected_nameservers = sorted(item.strip().lower() for item in os.environ["PLAN_ZONE_NAMESERVERS"].split(",") if item.strip())
google_idps = [item for item in idps if item.get("type") == "google-apps" and item.get("id")]
idp_status = "ready" if len(google_idps) == 1 else ("missing" if not google_idps else "ambiguous")
google_idp_id = google_idps[0]["id"] if len(google_idps) == 1 else ""

control_domain = os.environ["PLAN_CONTROL_HOSTNAME"]
wildcard_domain = os.environ["PLAN_WILDCARD_HOSTNAME"]
webhook_domain = os.environ["PLAN_GITHUB_WEBHOOK_HOSTNAME"]
worker_name = "madup-infra-manager-edge-worker"

zone_status = "ready" if zone and zone.get("name") == os.environ["PLAN_ZONE_NAME"] and zone.get("account", {}).get("id") == os.environ["PLAN_ACCOUNT_ID"] and zone.get("status") == "active" else "mismatch"
zone_nameservers_status = "ready" if zone and normalize_nameservers(zone.get("name_servers")) == expected_nameservers else "mismatch"
organization_status = "ready" if org and (org.get("auth_domain") or "").lower() == expected_auth_domain else "mismatch"

control_access_app_status = "mismatch" if os.environ.get("PLAN_CONTROL_APP_INVENTORY_STATUS") == "duplicate" else (exact_app_status(control_app, app_payload(control_domain, [google_idp_id], True), project_control_app) if google_idp_id else "missing")
wildcard_access_app_status = "mismatch" if os.environ.get("PLAN_WILDCARD_APP_INVENTORY_STATUS") == "duplicate" else (exact_app_status(wildcard_app, app_payload(wildcard_domain, [google_idp_id], False), project_wildcard_app) if google_idp_id else "missing")
control_access_policy_status = exact_policy_status(control_policies, google_idp_id) if google_idp_id else "missing"
wildcard_access_policy_status = exact_policy_status(wildcard_policies, google_idp_id) if google_idp_id else "missing"
if os.environ.get("PLAN_WEBHOOK_APP_INVENTORY_STATUS") == "duplicate":
    webhook_access_app_status = "mismatch"
elif webhook_app is None:
    webhook_access_app_status = "missing"
elif exact_payload_status(webhook_app, webhook_app_payload(webhook_domain), project_webhook_app):
    webhook_access_app_status = "ready"
else:
    webhook_access_app_status = "mismatch"
if not webhook_policies:
    webhook_access_policy_status = "missing"
elif len(webhook_policies) != 1:
    webhook_access_policy_status = "mismatch"
elif exact_payload_status(webhook_policies[0], webhook_policy_payload(), project_access_policy):
    webhook_access_policy_status = "ready"
else:
    webhook_access_policy_status = "mismatch"

control_dns_status = exact_dns_status(control_dns_records, control_domain, os.environ.get("PLAN_CONTROL_DNS_TARGET", ""))
wildcard_dns_status = exact_dns_status(wildcard_dns_records, wildcard_domain, os.environ.get("PLAN_APP_DNS_TARGET", ""))
control_route_status = exact_route_status(routes, os.environ["PLAN_CONTROL_ROUTE_PATTERN"], worker_name)
wildcard_route_status = exact_route_status(routes, os.environ["PLAN_WILDCARD_ROUTE_PATTERN"], worker_name)
shadow_routes = [
    {"id": route.get("id"), "pattern": route.get("pattern"), "script": route.get("script")}
    for route in routes
    if route.get("pattern") not in {os.environ["PLAN_CONTROL_ROUTE_PATTERN"], os.environ["PLAN_WILDCARD_ROUTE_PATTERN"]}
    and overlaps_managed_route(route.get("pattern"))
]
route_shadow_status = "mismatch" if shadow_routes else "ready"
worker_status = "ready" if any(item.get("id") == worker_name for item in scripts) else "missing"
managed_access_app_overlap_status = os.environ.get("PLAN_MANAGED_ACCESS_APP_OVERLAP_STATUS", "ready")
managed_access_app_overlap_json = os.environ.get("PLAN_MANAGED_ACCESS_APP_OVERLAP_JSON", "")
managed_access_app_overlap_apps = json.loads(Path(managed_access_app_overlap_json).read_text()) if managed_access_app_overlap_json and Path(managed_access_app_overlap_json).exists() else []

secret_names = sorted(item.get("name") for item in secrets if isinstance(item.get("name"), str))
expected_secret_names = sorted([
    os.environ["PLAN_CONTROL_SECRET_NAME"],
    os.environ["PLAN_APP_SECRET_NAME"],
])
if worker_status != "ready":
    worker_secret_binding_status = "missing"
elif not secrets:
    worker_secret_binding_status = "missing"
elif secret_names == expected_secret_names and all(item.get("type") == "secret_text" for item in secrets):
    worker_secret_binding_status = "ready"
else:
    worker_secret_binding_status = "mismatch"

control_audience = (control_app or {}).get("aud") or ""
wildcard_audience = (wildcard_app or {}).get("aud") or ""
if control_audience and wildcard_audience and control_audience != wildcard_audience:
    audience_distinct_status = "ready"
elif control_audience or wildcard_audience:
    audience_distinct_status = "mismatch"
else:
    audience_distinct_status = "missing"

initial_state = {
    "access_seat_count": seat_count,
    "zone_status": zone_status,
    "zone_nameservers_status": zone_nameservers_status,
    "organization_status": organization_status,
    "google_workspace_idp_status": idp_status,
    "google_workspace_idp_ids": [item["id"] for item in google_idps],
    "control_access_app_inventory_status": os.environ.get("PLAN_CONTROL_APP_INVENTORY_STATUS", "missing"),
    "wildcard_access_app_inventory_status": os.environ.get("PLAN_WILDCARD_APP_INVENTORY_STATUS", "missing"),
    "github_webhook_access_app_inventory_status": os.environ.get("PLAN_WEBHOOK_APP_INVENTORY_STATUS", "missing"),
    "control_access_app_status": control_access_app_status,
    "control_access_policy_status": control_access_policy_status,
    "wildcard_access_app_status": wildcard_access_app_status,
    "wildcard_access_policy_status": wildcard_access_policy_status,
    "github_webhook_access_app_status": webhook_access_app_status,
    "github_webhook_access_policy_status": webhook_access_policy_status,
    "managed_access_app_overlap_status": managed_access_app_overlap_status,
    "managed_access_app_overlap_apps": managed_access_app_overlap_apps,
    "project_number_status": "ready" if os.environ["PLAN_PROJECT_NUMBER_VALID"] == "true" else "mismatch",
    "control_access_app_id": None if control_app is None else control_app.get("id"),
    "wildcard_access_app_id": None if wildcard_app is None else wildcard_app.get("id"),
    "github_webhook_access_app_id": None if webhook_app is None else webhook_app.get("id"),
    "control_access_policy_id": None if not control_policies else control_policies[0].get("id"),
    "wildcard_access_policy_id": None if not wildcard_policies else wildcard_policies[0].get("id"),
    "github_webhook_access_policy_id": None if not webhook_policies else webhook_policies[0].get("id"),
    "control_access_audience": control_audience,
    "wildcard_access_audience": wildcard_audience,
    "audience_distinct_status": audience_distinct_status,
    "control_dns_record_status": control_dns_status,
    "wildcard_dns_record_status": wildcard_dns_status,
    "control_dns_record_id": None if len(control_dns_records) != 1 else control_dns_records[0].get("id"),
    "wildcard_dns_record_id": None if len(wildcard_dns_records) != 1 else wildcard_dns_records[0].get("id"),
    "control_worker_route_status": control_route_status,
    "wildcard_worker_route_status": wildcard_route_status,
    "route_shadow_status": route_shadow_status,
    "route_shadow_patterns": shadow_routes,
    "worker_status": worker_status,
    "worker_secret_binding_status": worker_secret_binding_status,
}
discovery_hash = hashlib.sha256(json.dumps(initial_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

blockers = []
if seat_count >= 50:
    blockers.append({"code": "cloudflare-access-pilot-seat-limit-exceeded", "message": "Cloudflare Access pilot seat count must stay below 50 users."})
if zone_status != "ready":
    blockers.append({"code": "cloudflare-zone-mismatch", "message": "The configured zone must resolve to the exact company Cloudflare account and madup.app zone."})
if zone_nameservers_status != "ready":
    blockers.append({"code": "cloudflare-zone-nameservers-mismatch", "message": "The configured zone must use the exact authoritative Cloudflare nameservers for madup.app."})
if organization_status != "ready":
    blockers.append({"code": "cloudflare-team-domain-mismatch", "message": "The Zero Trust auth domain must match the configured company team name."})
if idp_status != "ready":
    blockers.append({"code": "google-workspace-idp-required", "message": "Managed OAuth requires exactly one Google Workspace identity provider in the company account."})
if control_access_app_status == "mismatch":
    blockers.append({"code": "control-access-app-required", "message": "The control self-hosted Access application must exist with exact managed OAuth settings."})
if control_access_policy_status == "mismatch":
    blockers.append({"code": "control-access-policy-required", "message": "The control Access application must have exactly one gsuite allow policy for mim-users@madup.com."})
if wildcard_access_app_status == "mismatch":
    blockers.append({"code": "wildcard-access-app-required", "message": "The wildcard self-hosted Access application must exist with the exact Google Workspace identity provider."})
if wildcard_access_policy_status == "mismatch":
    blockers.append({"code": "wildcard-access-policy-required", "message": "The wildcard Access application must have exactly one gsuite allow policy for mim-users@madup.com."})
if webhook_access_app_status == "mismatch":
    blockers.append({"code": "github-webhook-access-app-required", "message": "The GitHub webhook Access application must exist on the exact narrow webhook path."})
if webhook_access_policy_status == "mismatch":
    blockers.append({"code": "github-webhook-bypass-policy-required", "message": "The GitHub webhook Access application must have exactly one narrow bypass-everyone policy."})
if os.environ.get("PLAN_CONTROL_APP_INVENTORY_STATUS") == "duplicate":
    blockers.append({"code": "control-access-app-duplicate-detected", "message": "Multiple exact control Access applications were discovered."})
if os.environ.get("PLAN_WILDCARD_APP_INVENTORY_STATUS") == "duplicate":
    blockers.append({"code": "wildcard-access-app-duplicate-detected", "message": "Multiple exact wildcard Access applications were discovered."})
if os.environ.get("PLAN_WEBHOOK_APP_INVENTORY_STATUS") == "duplicate":
    blockers.append({"code": "github-webhook-access-app-duplicate-detected", "message": "Multiple exact GitHub webhook Access applications were discovered."})
if managed_access_app_overlap_status != "ready":
    blockers.append({"code": "managed-access-app-overlap-detected", "message": "An extra Access application overlaps the managed madup.app namespace or infra-manager naming prefix."})
if audience_distinct_status == "mismatch":
    blockers.append({"code": "access-app-audience-distinct-required", "message": "Control and wildcard Access applications must expose distinct non-empty audience tags."})
if os.environ["PLAN_CONTROL_ORIGIN_VALID"] != "true":
    blockers.append({"code": "edge-origin-url-required", "message": "MIM_TASK18_EDGE_ORIGIN_URL must be an exact HTTPS Cloud Run run.app origin."})
if os.environ["PLAN_APP_ORIGIN_VALID"] != "true":
    blockers.append({"code": "app-gateway-origin-url-required", "message": "MIM_TASK18_APP_GATEWAY_ORIGIN_URL must be the exact deterministic app-gateway Cloud Run run.app origin."})
if os.environ["PLAN_DISTINCT_ORIGIN_VALID"] != "true":
    blockers.append({"code": "origin-distinct-required", "message": "Control and app-gateway origins must both be configured and must not be identical."})
if os.environ["PLAN_CONTROL_KEY_VALID"] != "true":
    blockers.append({"code": "control-origin-key-id-required", "message": "MIM_TASK18_EDGE_ORIGIN_HMAC_KEY_ID must be a safe non-empty Worker key identifier."})
if os.environ["PLAN_APP_KEY_VALID"] != "true":
    blockers.append({"code": "app-gateway-origin-key-id-required", "message": "MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_KEY_ID must be a safe non-empty Worker key identifier."})
if os.environ["PLAN_CONTROL_SECRET_VALID"] != "true":
    blockers.append({"code": "control-origin-secret-required", "message": "MIM_TASK18_EDGE_ORIGIN_HMAC_SECRET must be present and at least 32 bytes."})
if os.environ["PLAN_APP_SECRET_VALID"] != "true":
    blockers.append({"code": "app-gateway-origin-secret-required", "message": "MIM_TASK18_APP_GATEWAY_ORIGIN_HMAC_SECRET must be present and at least 32 bytes."})
if os.environ["PLAN_PROJECT_NUMBER_VALID"] != "true":
    blockers.append({"code": "project-number-required", "message": "MIM_PROJECT_NUMBER must be the exact reviewed 12-digit Cloud project number for Worker origin derivation."})
if control_dns_status in {"mismatch", "ambiguous"}:
    blockers.append({"code": "control-dns-record-required", "message": "mim.madup.app must point at the exact reviewed control Cloud Run hostname as a proxied CNAME."})
if wildcard_dns_status in {"mismatch", "ambiguous"}:
    blockers.append({"code": "wildcard-dns-record-required", "message": "*.madup.app must point at the exact reviewed app-gateway Cloud Run hostname as a proxied CNAME."})
if control_route_status in {"mismatch", "ambiguous"}:
    blockers.append({"code": "control-worker-route-required", "message": "The control Worker route must map mim.madup.app/* to the reviewed Worker script."})
if wildcard_route_status in {"mismatch", "ambiguous"}:
    blockers.append({"code": "wildcard-worker-route-required", "message": "The wildcard Worker route must map *.madup.app/* to the reviewed Worker script."})
if route_shadow_status != "ready":
    blockers.append({"code": "worker-route-shadow-detected", "message": "The zone contains an overlapping Worker route inside the managed madup.app namespace."})
if worker_secret_binding_status == "mismatch":
    blockers.append({"code": "worker-secret-binding-required", "message": "The Worker must expose exactly the two reviewed HMAC secret bindings and nothing else."})

status = "blocked" if blockers else "ready"
actions = []
if google_idp_id:
    control_app_payload = app_payload(control_domain, [google_idp_id], True)
    wildcard_app_payload = app_payload(wildcard_domain, [google_idp_id], False)
    control_policy_payload = policy_payload(google_idp_id)
    wildcard_policy_payload = policy_payload(google_idp_id)
    webhook_payload = webhook_app_payload(webhook_domain)
    webhook_policy = webhook_policy_payload()
else:
    control_app_payload = {}
    wildcard_app_payload = {}
    control_policy_payload = {}
    wildcard_policy_payload = {}
    webhook_payload = {}
    webhook_policy = {}

def missing_before_state():
    return {"status": "missing"}

if status == "ready":
    if control_access_app_status == "missing":
        actions.append({
            "kind": "upsert_access_app",
            "hostname": control_domain,
            "domain": control_domain,
            "app_id": None,
            "payload": control_app_payload,
            "before_state": missing_before_state(),
        })
    if control_access_policy_status == "missing":
        actions.append({
            "kind": "upsert_access_policy",
            "hostname": control_domain,
            "app_id": None if control_app is None else control_app.get("id"),
            "policy_id": None,
            "payload": control_policy_payload,
            "before_state": missing_before_state(),
        })
    if wildcard_access_app_status == "missing":
        actions.append({
            "kind": "upsert_access_app",
            "hostname": wildcard_domain,
            "domain": wildcard_domain,
            "app_id": None,
            "payload": wildcard_app_payload,
            "before_state": missing_before_state(),
        })
    if wildcard_access_policy_status == "missing":
        actions.append({
            "kind": "upsert_access_policy",
            "hostname": wildcard_domain,
            "app_id": None if wildcard_app is None else wildcard_app.get("id"),
            "policy_id": None,
            "payload": wildcard_policy_payload,
            "before_state": missing_before_state(),
        })
    if webhook_access_app_status == "missing":
        actions.append({
            "kind": "upsert_access_app",
            "hostname": webhook_domain,
            "domain": webhook_domain,
            "app_id": None,
            "payload": webhook_payload,
            "before_state": missing_before_state(),
        })
    if webhook_access_policy_status == "missing":
        actions.append({
            "kind": "upsert_access_policy",
            "hostname": webhook_domain,
            "app_id": None if webhook_app is None else webhook_app.get("id"),
            "policy_id": None,
            "payload": webhook_policy,
            "before_state": missing_before_state(),
        })
    if worker_status == "missing" or worker_secret_binding_status == "missing":
        actions.append({
            "kind": "deploy_worker",
            "worker_name": worker_name,
            "control_hostname": control_domain,
            "app_host_suffix": os.environ["PLAN_APP_HOST_SUFFIX"],
            "control_origin_url": os.environ["PLAN_CONTROL_ORIGIN_URL"],
            "app_gateway_origin_url": os.environ["PLAN_APP_GATEWAY_ORIGIN_URL"],
            "control_origin_hmac_key_id": os.environ["PLAN_CONTROL_KEY_ID"],
            "app_gateway_origin_hmac_key_id": os.environ["PLAN_APP_KEY_ID"],
            "required_secret_names": [
                os.environ["PLAN_CONTROL_SECRET_NAME"],
                os.environ["PLAN_APP_SECRET_NAME"],
            ],
            "control_allowed_routes": [
                "POST /mcp",
                "GET /healthz",
                "GET /readyz",
                "GET /mcp/ws",
                "GET /v1/operations/{id}",
                "GET /v1/failures/{id}",
                "GET /v1/workloads",
                "GET /v1/usage",
                "GET /v1/plan/deploy?*",
                "GET /v1/plan/schedule?*",
                "GET /dashboard",
                "GET /static/{asset}",
                "GET /v1/secrets/handoff?*",
                "POST /v1/deployments",
                "POST /v1/schedules",
                "POST /v1/schedules/{schedule_id}/pause",
                "POST /v1/schedules/{schedule_id}/resume",
                "POST /v1/secrets/handoff?*",
                "POST /v1/webhooks/github",
            ],
            "before_state": {
                "worker_status": worker_status,
                "worker_secret_binding_status": worker_secret_binding_status,
            },
            "project_number": os.environ["PLAN_PROJECT_NUMBER"],
        })
    if control_dns_status == "missing":
        actions.append({
            "kind": "upsert_dns_record",
            "record_type": "CNAME",
            "hostname": control_domain,
            "target_hostname": os.environ["PLAN_CONTROL_DNS_TARGET"],
            "proxied": True,
            "before_state": missing_before_state(),
        })
    if wildcard_dns_status == "missing":
        actions.append({
            "kind": "upsert_dns_record",
            "record_type": "CNAME",
            "hostname": wildcard_domain,
            "target_hostname": os.environ["PLAN_APP_DNS_TARGET"],
            "proxied": True,
            "before_state": missing_before_state(),
        })
    if control_route_status == "missing":
        actions.append({
            "kind": "upsert_worker_route",
            "pattern": os.environ["PLAN_CONTROL_ROUTE_PATTERN"],
            "worker_name": worker_name,
            "before_state": missing_before_state(),
        })
    if wildcard_route_status == "missing":
        actions.append({
            "kind": "upsert_worker_route",
            "pattern": os.environ["PLAN_WILDCARD_ROUTE_PATTERN"],
            "worker_name": worker_name,
            "before_state": missing_before_state(),
        })

plan = {
    "version": "mim-edge-plan-v3",
    "generated_at_epoch": int(os.environ["PLAN_GENERATED_AT"]),
    "expires_at_epoch": int(os.environ["PLAN_EXPIRES_AT"]),
    "status": status,
    "blockers": blockers,
    "config": {
        "operator_email": os.environ["PLAN_OPERATOR_EMAIL"],
        "project_id": os.environ["PLAN_PROJECT_ID"],
        "organization_id": os.environ["PLAN_ORGANIZATION_ID"],
        "billing_account_id": os.environ["PLAN_BILLING_ACCOUNT_ID"],
        "config_fingerprint": os.environ["PLAN_CONFIG_FINGERPRINT"],
    },
    "targets": {
        "control_hostname": control_domain,
        "wildcard_hostname": wildcard_domain,
        "zone_name": os.environ["PLAN_ZONE_NAME"],
        "authoritative_nameservers": expected_nameservers,
        "team_auth_domain": expected_auth_domain,
        "google_idp_provider": "google-workspace",
        "allowed_group_email": os.environ["PLAN_ALLOWED_GROUP_EMAIL"],
        "github_webhook_hostname": webhook_domain,
        "worker_name": worker_name,
        "app_host_suffix": os.environ["PLAN_APP_HOST_SUFFIX"],
        "control_origin_url": os.environ["PLAN_CONTROL_ORIGIN_URL"],
        "app_gateway_origin_url": os.environ["PLAN_APP_GATEWAY_ORIGIN_URL"],
        "project_number": os.environ["PLAN_PROJECT_NUMBER"],
        "control_dns_target_hostname": os.environ["PLAN_CONTROL_DNS_TARGET"],
        "wildcard_dns_target_hostname": os.environ["PLAN_APP_DNS_TARGET"],
        "control_route_pattern": os.environ["PLAN_CONTROL_ROUTE_PATTERN"],
        "wildcard_route_pattern": os.environ["PLAN_WILDCARD_ROUTE_PATTERN"],
        "managed_oauth_settings": {
            "allow_any_on_localhost": True,
            "allow_any_on_loopback": True,
            "allowed_uris": [],
            "access_token_lifetime": os.environ["PLAN_MANAGED_OAUTH_ACCESS_TOKEN_LIFETIME"],
            "session_duration": os.environ["PLAN_MANAGED_OAUTH_SESSION_DURATION"],
        },
        "required_worker_secret_names": [
            os.environ["PLAN_CONTROL_SECRET_NAME"],
            os.environ["PLAN_APP_SECRET_NAME"],
        ],
        "gabia_mutation": False,
    },
    "initial_state": initial_state,
    "constraints": {
        "only_hostnames": [control_domain, wildcard_domain],
        "no_apex_change": True,
        "no_broad_delete": True,
        "route_fail_mode_closed_readback": True,
    },
    "actions": actions,
    "discovery_hash": discovery_hash,
}
Path(sys.argv[1]).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
PY

mim_task18_write_plan_json "$PLAN_PATH" "$PLAN_OUT"
printf 'Wrote reviewed plan to %s\n' "$PLAN_OUT"
