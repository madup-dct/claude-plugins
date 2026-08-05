#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"
CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"

MODE=
PLAN_FILE=
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --apply) MODE=apply; shift ;;
    --plan-file) PLAN_FILE=$2; shift 2 ;;
    --*) mim_task18_fail "Unknown argument: $1" ;;
    *) mim_task18_fail "Positional arguments are not supported" ;;
  esac
done
[[ "$MODE" == apply && -n "$PLAN_FILE" ]] || mim_task18_fail "Usage: apply_connection.sh --apply --plan-file .state/<name>.json"
mim_task18_assert_plan_read_path "$SCRIPT_DIR" "$PLAN_FILE"
mim_task18_validate_plan_hash_and_age "$PLAN_FILE"
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
EXPECTED_PATH="$SCRIPT_DIR/.state/task18-github-expected-$$.json"
trap 'rm -rf "$TMP_DIR" "$SNAPSHOT_DIR"; rm -f "$EXPECTED_PATH" "$EXPECTED_PATH.sha256"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
MIM_CONFIG_FILE="$SNAPSHOT_CONFIG"
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v gh >/dev/null 2>&1 || mim_task18_fail "gh CLI is required"
command -v gcloud >/dev/null 2>&1 || mim_task18_fail "gcloud CLI is required"
ACTIVE_ACCOUNT=$(mim_task18_assert_active_gcloud_account)

MIM_CONFIG_FILE="$SNAPSHOT_CONFIG" \
MIM_TASK18_PLAN_GENERATED_AT="$PLAN_GENERATED_AT" \
MIM_TASK18_PLAN_EXPIRES_AT="$PLAN_EXPIRES_AT" \
bash "$SCRIPT_DIR/plan_connection.sh" --plan --out "$EXPECTED_PATH" >/dev/null
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
import json, sys
from pathlib import Path
for action in json.loads(Path(sys.argv[1]).read_text())["actions"]:
    if action["kind"] == "create_connection":
        print(f'create_connection\t{action["name"]}\t{action["authorizer_token_secret_version"]}\t{action["app_installation_id"]}')
    elif action["kind"] == "create_repository_resource":
        print(f'create_repository_resource\t{action["name"]}\t{action["remote_uri"]}')
    elif action["kind"] == "configure_webhook":
        print(f'configure_webhook\t{action["repository_id"]}\t{action["owner"]}\t{action["name"]}\t{action["webhook_url"]}')
PY

plan_secret_version=$(python3 - "$PLAN_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["runtime_secret_contract"]["secret_version"])
PY
)
IFS=$'\t' read -r plan_secret_name plan_secret_number <<<"$(PLAN_SECRET_VERSION="$plan_secret_version" python3 - <<'PY'
import os, re
value = os.environ["PLAN_SECRET_VERSION"]
match = re.fullmatch(
    r"projects/([a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]*)/secrets/(mim-github-webhook)/versions/([1-9][0-9]*)",
    value,
)
if match is None:
    raise SystemExit(1)
print(f"{match.group(2)}\t{match.group(3)}")
PY
)" || mim_task18_fail "Plan file does not match the expected reviewed contract"

ensure_repo_admin() {
  local owner=$1 repo=$2
  local repo_json
  repo_json=$(gh api "/repos/$owner/$repo") || mim_task18_fail "GitHub mutation credential could not inspect $owner/$repo"
  if ! REPO_JSON="$repo_json" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["REPO_JSON"])
permissions = payload.get("permissions")
if not isinstance(permissions, dict) or permissions.get("admin") is not True:
    raise SystemExit(1)
PY
  then
    mim_task18_fail "GitHub mutation credential must have repository admin on $owner/$repo"
  fi
}

create_webhook_body() {
  local output_path=$1 webhook_url=$2 secret_path=$3
  WEBHOOK_URL="$webhook_url" python3 - "$output_path" "$secret_path" <<'PY'
import json, os, sys
from pathlib import Path
webhook_secret = Path(sys.argv[2]).read_text()
payload = {
    "name": "web",
    "active": True,
    "events": ["push"],
    "config": {
        "url": os.environ["WEBHOOK_URL"],
        "content_type": "json",
        "secret": webhook_secret,
        "insecure_ssl": "0",
    },
}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
}

discover_single_hook() {
  local owner=$1 repo=$2
  local hooks_json
  hooks_json=$(gh api "/repos/$owner/$repo/hooks") || mim_task18_fail "Unable to inspect existing GitHub webhooks for $owner/$repo"
  HOOKS_JSON="$hooks_json" GITHUB_WEBHOOK_URL="$MIM_TASK18_GITHUB_WEBHOOK_URL" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["HOOKS_JSON"])
hooks = []
for item in payload:
    if isinstance(item, dict):
        config = item.get("config")
        if isinstance(config, dict) and config.get("url") == os.environ["GITHUB_WEBHOOK_URL"]:
            hooks.append(item)
if len(hooks) > 1:
    print("duplicate")
elif len(hooks) == 0:
    print("absent")
else:
    hook = hooks[0]
    print("\t".join([
        "present",
        str(hook.get("id", "")),
        "true" if hook.get("active") is True else "false",
        ",".join(hook.get("events") or []),
        str((hook.get("config") or {}).get("content_type", "")),
        str((hook.get("config") or {}).get("insecure_ssl", "")),
    ]))
PY
}

hook_matches_contract() {
  local hook_json=$1
  HOOK_JSON="$hook_json" GITHUB_WEBHOOK_URL="$MIM_TASK18_GITHUB_WEBHOOK_URL" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["HOOK_JSON"])
config = payload.get("config")
if not isinstance(config, dict):
    raise SystemExit(1)
if payload.get("active") is not True:
    raise SystemExit(1)
if payload.get("events") != ["push"]:
    raise SystemExit(1)
if config.get("url") != os.environ["GITHUB_WEBHOOK_URL"]:
    raise SystemExit(1)
if config.get("content_type") != "json":
    raise SystemExit(1)
if config.get("insecure_ssl") != "0":
    raise SystemExit(1)
PY
}

readback_hook() {
  local owner=$1 repo=$2 hook_id=$3
  gh api "/repos/$owner/$repo/hooks/$hook_id"
}

webhook_secret_resource_state=$(mim_task18_gcloud_optional_output \
  "Unable to inspect the GitHub webhook runtime secret" \
  "$TMP_DIR/webhook-secret-resource.txt" \
  secrets describe "$MIM_TASK18_GITHUB_WEBHOOK_SECRET_NAME" \
  '--format=value(name)' \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID")
if [[ "$webhook_secret_resource_state" == "missing" ]]; then
  gcloud secrets create "$MIM_TASK18_GITHUB_WEBHOOK_SECRET_NAME" \
    --replication-policy=automatic \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" >/dev/null
fi
final_webhook_secret_version="$plan_secret_version"
webhook_secret_path="$TMP_DIR/webhook-secret.txt"

while IFS=$'\t' read -r kind a b c d; do
  [[ -n "$kind" ]] || continue
  case "$kind" in
    create_connection)
      gcloud builds connections create github "$a" \
        --project="$MIM_PROJECT_ID" \
        --region="$MIM_TASK18_FIXED_REGION" \
        --authorizer-token-secret-version="$b" \
        --app-installation-id="$c" \
        --account="$MIM_OPERATOR_EMAIL" >/dev/null
      ;;
    create_repository_resource)
      gcloud builds repositories create "$a" \
        --connection="$MIM_TASK18_GITHUB_CONNECTION_NAME" \
        --region="$MIM_TASK18_FIXED_REGION" \
        --project="$MIM_PROJECT_ID" \
        --remote-uri="$b" \
        --account="$MIM_OPERATOR_EMAIL" >/dev/null
      ;;
    configure_webhook)
      hook_state=$(discover_single_hook "$b" "$c")
      [[ "$hook_state" != "duplicate" ]] || mim_task18_fail "Duplicate MIM GitHub webhook hooks are forbidden"
      if [[ "$hook_state" == "absent" ]]; then
        ensure_repo_admin "$b" "$c"
        gcloud secrets versions access "$plan_secret_number" \
          --secret="$plan_secret_name" \
          --out-file="$webhook_secret_path" \
          --account="$MIM_OPERATOR_EMAIL" \
          --project="$MIM_PROJECT_ID" >/dev/null
        webhook_body="$TMP_DIR/$c-hook.json"
        create_webhook_body "$webhook_body" "$d" "$webhook_secret_path"
        response=$(gh api "/repos/$b/$c/hooks" --method POST --input "$webhook_body")
      else
        IFS=$'\t' read -r _present hook_id hook_active hook_events hook_content_type hook_ssl <<<"$hook_state"
        if [[ "$hook_active" == "true" && "$hook_events" == "push" && "$hook_content_type" == "json" && "$hook_ssl" == "0" ]]; then
          continue
        else
          ensure_repo_admin "$b" "$c"
          gcloud secrets versions access "$plan_secret_number" \
            --secret="$plan_secret_name" \
            --out-file="$webhook_secret_path" \
            --account="$MIM_OPERATOR_EMAIL" \
            --project="$MIM_PROJECT_ID" >/dev/null
          webhook_body="$TMP_DIR/$c-hook.json"
          create_webhook_body "$webhook_body" "$d" "$webhook_secret_path"
          response=$(gh api "/repos/$b/$c/hooks/$hook_id" --method PATCH --input "$webhook_body")
        fi
      fi
      hook_id=$(HOOK_JSON="$response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["HOOK_JSON"])
print(payload.get("id", ""))
PY
)
      readback=$(readback_hook "$b" "$c" "$hook_id")
      hook_matches_contract "$readback" || mim_task18_fail "GitHub webhook verification failed for $b/$c"
      ;;
    *)
      mim_task18_fail "Plan file does not match the expected reviewed contract"
      ;;
  esac
done <"$TMP_DIR/actions.tsv"

printf 'Applied reviewed plan with GitHub webhook secret version %s.\n' "$final_webhook_secret_version"
