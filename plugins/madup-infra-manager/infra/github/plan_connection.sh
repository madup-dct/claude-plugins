#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"
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
[[ "$MODE" == plan && -n "$PLAN_OUT" ]] || mim_task18_fail "Usage: plan_connection.sh --plan --out .state/<name>.json"
mim_task18_assert_plan_create_path "$SCRIPT_DIR" "$PLAN_OUT"

TMP_DIR=$(mktemp -d)
SNAPSHOT_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR" "$SNAPSHOT_DIR"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v gh >/dev/null 2>&1 || mim_task18_fail "gh CLI is required"
command -v gcloud >/dev/null 2>&1 || mim_task18_fail "gcloud CLI is required"
ACTIVE_ACCOUNT=$(mim_task18_assert_active_gcloud_account)

IFS=',' read -r -a repo_ids <<<"$MIM_GITHUB_REPOSITORY_IDS"
[[ "${#repo_ids[@]}" -ge 1 ]] || mim_task18_fail "Invalid MIM_GITHUB_REPOSITORY_IDS"

REPO_ROWS_FILE="$TMP_DIR/repos.tsv"
REPOSITORY_RESOURCE_ROWS_FILE="$TMP_DIR/repository-resources.tsv"
HOOK_ROWS_FILE="$TMP_DIR/hooks.tsv"
SELECTED_IDS_FILE="$TMP_DIR/selected-ids.txt"
: >"$REPO_ROWS_FILE"
: >"$REPOSITORY_RESOURCE_ROWS_FILE"
: >"$HOOK_ROWS_FILE"

installation_status=exact
installation_id=
installation_account_login=
installation_repository_selection=
installation_target_type=
installation_permissions_json=

discover_hook_state() {
  local owner=$1 repo=$2
  local hooks_json hook_row
  hooks_json=$(gh api "/repos/$owner/$repo/hooks") || mim_task18_fail "Unable to inspect existing GitHub webhooks for $owner/$repo"
  hook_row=$(HOOKS_JSON="$hooks_json" GITHUB_WEBHOOK_URL="$MIM_TASK18_GITHUB_WEBHOOK_URL" python3 - <<'PY'
import json, os

payload = json.loads(os.environ["HOOKS_JSON"])
if not isinstance(payload, list):
    raise SystemExit("invalid hooks payload")
mim_hooks = []
for item in payload:
    if not isinstance(item, dict):
        raise SystemExit("invalid hook entry")
    config = item.get("config")
    if not isinstance(config, dict):
        raise SystemExit("invalid hook config")
    if config.get("url") == os.environ["GITHUB_WEBHOOK_URL"]:
        mim_hooks.append(item)
if len(mim_hooks) > 1:
    print("duplicate")
elif len(mim_hooks) == 0:
    print("absent")
else:
    hook = mim_hooks[0]
    config = hook["config"]
    events = hook.get("events")
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        raise SystemExit("invalid hook events")
    print(
        "\t".join(
            [
                "present",
                str(hook.get("id", "")),
                "true" if hook.get("active") is True else "false",
                ",".join(events),
                str(config.get("content_type", "")),
                str(config.get("insecure_ssl", "")),
            ]
        )
    )
PY
)
  if [[ "$hook_row" == "duplicate" ]]; then
    mim_task18_fail "Duplicate MIM GitHub webhook hooks are forbidden"
  fi
  if [[ "$hook_row" == "absent" ]]; then
    printf '%s\tabsent\t\t\t\t\n' "$repo" >>"$HOOK_ROWS_FILE"
    return
  fi
  IFS=$'\t' read -r hook_status hook_id hook_active hook_events hook_content_type hook_ssl <<<"$hook_row"
  if [[ "$hook_active" == "true" && "$hook_events" == "push" && "$hook_content_type" == "json" && "$hook_ssl" == "0" ]]; then
    printf '%s\texact\t%s\t%s\t%s\t%s\n' "$repo" "$hook_id" "$hook_active" "$hook_events" "$hook_ssl" >>"$HOOK_ROWS_FILE"
  else
    printf '%s\tdrift\t%s\t%s\t%s\t%s\n' "$repo" "$hook_id" "$hook_active" "$hook_events" "$hook_ssl" >>"$HOOK_ROWS_FILE"
  fi
}

CONFIGURED_PROJECT_NUMBER=$(mim_task18_gcloud_capture \
  "Unable to inspect the configured project number" \
  projects describe "$MIM_PROJECT_ID" \
  --account="$MIM_OPERATOR_EMAIL" \
  '--format=value(projectNumber)')
[[ "$CONFIGURED_PROJECT_NUMBER" =~ ^[1-9][0-9]*$ ]] || mim_task18_fail "Configured project number must be numeric"

resolve_exact_secret_version() {
  local label=$1 secret_name=$2 override_env=$3
  local explicit_ref=${!override_env-}
  local payload_file="$TMP_DIR/$secret_name-secret-version.json"
  local state version_number parsed_ref
  local describe_error="Unable to inspect the $label secret version"
  local list_error="Unable to inspect enabled $label secret versions"

  if [[ -n "$explicit_ref" ]]; then
    parsed_ref=$(EXPLICIT_REF="$explicit_ref" SECRET_NAME="$secret_name" PROJECT_ID="$MIM_PROJECT_ID" PROJECT_NUMBER="$CONFIGURED_PROJECT_NUMBER" python3 - <<'PY'
import os
import re

value = os.environ["EXPLICIT_REF"]
match = re.fullmatch(
    r"projects/([^/]+)/secrets/([^/]+)/versions/([1-9][0-9]*)",
    value,
)
if match is None:
    raise SystemExit("invalid")
project_token, secret_name, version_number = match.groups()
allowed_projects = {os.environ["PROJECT_ID"], os.environ["PROJECT_NUMBER"]}
if project_token not in allowed_projects:
    raise SystemExit("project")
if secret_name != os.environ["SECRET_NAME"]:
    raise SystemExit("secret")
print(f"{version_number}\t{project_token}")
PY
    ) || mim_task18_fail "$override_env must reference the configured project, expected secret name, and an exact numeric version resource"
    IFS=$'\t' read -r version_number _explicit_project <<<"$parsed_ref"
    state=$(mim_task18_gcloud_optional_output \
      "$describe_error" \
      "$payload_file" \
      secrets versions describe "$version_number" \
      --secret="$secret_name" \
      --account="$MIM_OPERATOR_EMAIL" \
      --project="$MIM_PROJECT_ID" \
      '--format=json')
    [[ "$state" == "exists" ]] || mim_task18_fail "$override_env must reference an existing enabled secret version"
    if ! SECRET_VERSION_FILE="$payload_file" SECRET_NAME="$secret_name" PROJECT_ID="$MIM_PROJECT_ID" PROJECT_NUMBER="$CONFIGURED_PROJECT_NUMBER" VERSION_NUMBER="$version_number" python3 - <<'PY'
import json
import os
import re
from pathlib import Path

payload = json.loads(Path(os.environ["SECRET_VERSION_FILE"]).read_text())
name = payload.get("name", "")
state = payload.get("state", "")
match = re.fullmatch(
    r"projects/([^/]+)/secrets/([^/]+)/versions/([1-9][0-9]*)",
    name,
)
if match is None:
    raise SystemExit("invalid")
project_token, secret_name, version_number = match.groups()
allowed_projects = {os.environ["PROJECT_ID"], os.environ["PROJECT_NUMBER"]}
if project_token not in allowed_projects:
    raise SystemExit("project")
if secret_name != os.environ["SECRET_NAME"]:
    raise SystemExit("secret")
if version_number != os.environ["VERSION_NUMBER"]:
    raise SystemExit("version")
if state != "ENABLED":
    raise SystemExit("state")
print(f"exists\t{name}\t{state}")
PY
    then
      mim_task18_fail "$override_env must reference an enabled secret version in the configured project"
    fi
    return
  fi

  state=$(mim_task18_gcloud_optional_output \
    "$list_error" \
    "$payload_file" \
    secrets versions list \
    --secret="$secret_name" \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" \
    '--filter=state:ENABLED' \
    '--format=json')
  if [[ "$state" == "missing" ]]; then
    printf 'missing\t\t\n'
    return
  fi

  if ! SECRET_VERSION_FILE="$payload_file" SECRET_NAME="$secret_name" PROJECT_ID="$MIM_PROJECT_ID" PROJECT_NUMBER="$CONFIGURED_PROJECT_NUMBER" python3 - <<'PY'
import json
import os
import re
from pathlib import Path

payload = json.loads(Path(os.environ["SECRET_VERSION_FILE"]).read_text())
if not isinstance(payload, list):
    raise SystemExit("invalid")

allowed_projects = {os.environ["PROJECT_ID"], os.environ["PROJECT_NUMBER"]}
secret_name = os.environ["SECRET_NAME"]
entries: list[tuple[str, str]] = []
for item in payload:
    if not isinstance(item, dict):
        raise SystemExit("invalid")
    name = item.get("name", "")
    state = item.get("state", "")
    match = re.fullmatch(
        r"projects/([^/]+)/secrets/([^/]+)/versions/([1-9][0-9]*)",
        name,
    )
    if match is None:
        raise SystemExit("invalid")
    project_token, discovered_secret_name, _version_number = match.groups()
    if project_token not in allowed_projects:
        raise SystemExit("project")
    if discovered_secret_name != secret_name:
        raise SystemExit("secret")
    if state != "ENABLED":
        raise SystemExit("state")
    entries.append((name, state))

if not entries:
    print("missing\t\t")
elif len(entries) > 1:
    print("ambiguous\t\t")
else:
    print(f"exists\t{entries[0][0]}\t{entries[0][1]}")
PY
  then
    mim_task18_fail "Unable to validate enabled $label secret versions"
  fi
}

for repo_id in "${repo_ids[@]}"; do
  repo_json=$(gh api "/repositories/$repo_id") || mim_task18_fail "Unable to inspect GitHub repository ID $repo_id"
  repo_row=$(REPO_JSON="$repo_json" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["REPO_JSON"])
repo_id = payload.get("id")
name = payload.get("name")
full_name = payload.get("full_name")
owner = (payload.get("owner") or {}).get("login")
fork = payload.get("fork")
html_url = payload.get("html_url")
if type(repo_id) is not int or repo_id <= 0 or type(name) is not str or not name or type(full_name) is not str or "/" not in full_name or type(owner) is not str or not owner or type(fork) is not bool or type(html_url) is not str:
    raise SystemExit(1)
print("\t".join([str(repo_id), owner, name, full_name, "true" if fork else "false", f"https://github.com/{owner}/{name}.git"]))
PY
)
  IFS=$'\t' read -r parsed_repo_id owner name full_name fork remote_uri <<<"$repo_row"
  [[ "$owner" == "madupmarketing" ]] || mim_task18_fail "Platform repository is forbidden"
  [[ "$full_name" != "madup-dct/claude-plugins" && "$name" != "claude-plugins" ]] || mim_task18_fail "Platform repository is forbidden"
  [[ "$fork" == "false" ]] || mim_task18_fail "Fork repositories are forbidden"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$parsed_repo_id" "$owner" "$name" "$full_name" "$fork" "$remote_uri" >>"$REPO_ROWS_FILE"
  discover_hook_state "$owner" "$name"

  if ! installation_json=$(gh api "/repos/$owner/$name/installation" 2>/dev/null); then
    installation_status=missing
    continue
  fi
  installation_row=$(INSTALLATION_JSON="$installation_json" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["INSTALLATION_JSON"])
print("\t".join([
    str(payload.get("id", "")),
    str((payload.get("account") or {}).get("login", "")),
    str(payload.get("repository_selection", "")),
    str(payload.get("target_type", "")),
    json.dumps(payload.get("permissions"), sort_keys=True, separators=(",", ":")),
]))
PY
)
  IFS=$'\t' read -r current_installation_id current_account_login current_repository_selection current_target_type current_permissions_json <<<"$installation_row"
  if [[ -z "$installation_id" ]]; then
    installation_id=$current_installation_id
    installation_account_login=$current_account_login
    installation_repository_selection=$current_repository_selection
    installation_target_type=$current_target_type
    installation_permissions_json=$current_permissions_json
  elif [[ "$installation_id" != "$current_installation_id" || "$installation_account_login" != "$current_account_login" || "$installation_repository_selection" != "$current_repository_selection" || "$installation_target_type" != "$current_target_type" || "$installation_permissions_json" != "$current_permissions_json" ]]; then
    installation_status=mismatch
  fi
done

if [[ "$installation_status" == "exact" && -n "$installation_id" ]]; then
  if selected_json=$(gh api "/user/installations/$installation_id/repositories?per_page=100"); then
    selected_repository_ids_csv=$(SELECTED_JSON="$selected_json" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["SELECTED_JSON"])
repos = payload.get("repositories")
if not isinstance(repos, list):
    raise SystemExit(1)
ids = sorted(int(item["id"]) for item in repos)
print(",".join(str(item) for item in ids))
PY
)
    printf '%s\n' "$selected_repository_ids_csv" | tr ',' '\n' | sed '/^$/d' >"$SELECTED_IDS_FILE"
    expected_ids_csv=$(printf '%s\n' "${repo_ids[@]}" | sort -n | paste -sd ',' -)
    if [[ "$installation_account_login" != "madupmarketing" || "$installation_repository_selection" != "selected" || "$installation_target_type" != "Organization" || "$installation_permissions_json" != '{"contents":"read","metadata":"read"}' || "$selected_repository_ids_csv" != "$expected_ids_csv" ]]; then
      installation_status=mismatch
    fi
  else
    installation_status=missing
  fi
fi

CONNECTION_JSON_FILE="$TMP_DIR/connection.json"
connection_state=$(mim_task18_gcloud_optional_output \
  "Unable to inspect the Cloud Build GitHub connection" \
  "$CONNECTION_JSON_FILE" \
  builds connections describe "$MIM_TASK18_GITHUB_CONNECTION_NAME" \
  --region="$MIM_TASK18_FIXED_REGION" \
  --account="$MIM_OPERATOR_EMAIL" \
  --project="$MIM_PROJECT_ID" \
  '--format=json')

connection_installation_id=
connection_authorizer_secret_version=
if [[ "$connection_state" == "exists" ]]; then
  IFS=$'\t' read -r connection_installation_id connection_authorizer_secret_version <<<"$(python3 - "$CONNECTION_JSON_FILE" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
config = payload.get("githubConfig") or {}
cred = config.get("authorizerCredential") or {}
print("\t".join([str(config.get("appInstallationId", "")), str(cred.get("oauthTokenSecretVersion", ""))]))
PY
)"
fi

IFS=$'\t' read -r authorizer_secret_state authorizer_secret_name authorizer_secret_version_state <<<"$(resolve_exact_secret_version \
  "GitHub authorizer token" \
  "$MIM_TASK18_GITHUB_AUTHORIZER_SECRET_NAME" \
  MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION)"
case "$authorizer_secret_state" in
  exists|missing) ;;
  ambiguous)
    mim_task18_fail "Multiple enabled GitHub authorizer token secret versions found; set MIM_TASK18_GITHUB_AUTHORIZER_SECRET_VERSION to an exact reviewed numeric version resource"
    ;;
  *)
    mim_task18_fail "Unable to resolve the GitHub authorizer token secret version contract"
    ;;
esac

IFS=$'\t' read -r webhook_secret_state webhook_secret_name webhook_secret_version_state <<<"$(resolve_exact_secret_version \
  "GitHub webhook" \
  "$MIM_TASK18_GITHUB_WEBHOOK_SECRET_NAME" \
  MIM_TASK18_GITHUB_WEBHOOK_SECRET_VERSION)"
case "$webhook_secret_state" in
  exists|missing) ;;
  ambiguous)
    mim_task18_fail "Multiple enabled GitHub webhook secret versions found; set MIM_TASK18_GITHUB_WEBHOOK_SECRET_VERSION to an exact reviewed numeric version resource"
    ;;
  *)
    mim_task18_fail "Unable to resolve the GitHub webhook secret version contract"
    ;;
esac

while IFS=$'\t' read -r _repo_id _owner name _full_name _fork remote_uri; do
  [[ -n "$name" ]] || continue
  repository_json_file="$TMP_DIR/repository-$name.json"
  repository_state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect the Cloud Build repository resource for $name" \
    "$repository_json_file" \
    builds repositories describe "$name" \
    --connection="$MIM_TASK18_GITHUB_CONNECTION_NAME" \
    --region="$MIM_TASK18_FIXED_REGION" \
    --account="$MIM_OPERATOR_EMAIL" \
    --project="$MIM_PROJECT_ID" \
    '--format=json')
  if [[ "$repository_state" == "exists" ]]; then
    actual_remote_uri=$(python3 - "$repository_json_file" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
print(payload.get("remoteUri", ""))
PY
)
    [[ "$actual_remote_uri" == "$remote_uri" ]] || mim_task18_fail "Cloud Build repository resource remote URI mismatch for $name"
  fi
  printf '%s\t%s\n' "$name" "$repository_state" >>"$REPOSITORY_RESOURCE_ROWS_FILE"
done <"$REPO_ROWS_FILE"

GENERATED_AT="${MIM_TASK18_PLAN_GENERATED_AT:-$(mim_task18_now_epoch)}"
EXPIRES_AT="${MIM_TASK18_PLAN_EXPIRES_AT:-$((GENERATED_AT + MIM_TASK18_PLAN_MAX_AGE_SECONDS))}"
CONFIG_FINGERPRINT=$(mim_task18_config_fingerprint "$SNAPSHOT_CONFIG")
EXPECTED_AUTHORIZER_SECRET_VERSION="$authorizer_secret_name"

PLAN_PATH="$TMP_DIR/github-plan.json"
PLAN_GENERATED_AT="$GENERATED_AT" \
PLAN_EXPIRES_AT="$EXPIRES_AT" \
PLAN_CONFIG_FINGERPRINT="$CONFIG_FINGERPRINT" \
PLAN_OPERATOR_EMAIL="$MIM_OPERATOR_EMAIL" \
PLAN_ACTIVE_ACCOUNT="$ACTIVE_ACCOUNT" \
PLAN_PROJECT_ID="$MIM_PROJECT_ID" \
PLAN_ORGANIZATION_ID="$MIM_ORGANIZATION_ID" \
PLAN_BILLING_ACCOUNT_ID="$MIM_BILLING_ACCOUNT_ID" \
PLAN_REPO_ROWS_FILE="$REPO_ROWS_FILE" \
PLAN_REPOSITORY_RESOURCE_ROWS_FILE="$REPOSITORY_RESOURCE_ROWS_FILE" \
PLAN_HOOK_ROWS_FILE="$HOOK_ROWS_FILE" \
PLAN_GITHUB_WEBHOOK_URL="$MIM_TASK18_GITHUB_WEBHOOK_URL" \
PLAN_CONNECTION_NAME="$MIM_TASK18_GITHUB_CONNECTION_NAME" \
PLAN_CONNECTION_STATE="$connection_state" \
PLAN_CONNECTION_INSTALLATION_ID="${connection_installation_id:-}" \
PLAN_CONNECTION_AUTHORIZER_SECRET_VERSION="${connection_authorizer_secret_version:-}" \
PLAN_INSTALLATION_STATUS="$installation_status" \
PLAN_INSTALLATION_ID="$installation_id" \
PLAN_INSTALLATION_ACCOUNT_LOGIN="$installation_account_login" \
PLAN_INSTALLATION_REPOSITORY_SELECTION="$installation_repository_selection" \
PLAN_INSTALLATION_TARGET_TYPE="$installation_target_type" \
PLAN_INSTALLATION_PERMISSIONS_JSON="$installation_permissions_json" \
PLAN_SELECTED_IDS_FILE="$SELECTED_IDS_FILE" \
PLAN_FIXED_REGION="$MIM_TASK18_FIXED_REGION" \
PLAN_AUTHORIZER_SECRET_STATE="$authorizer_secret_state" \
PLAN_AUTHORIZER_SECRET_NAME="${authorizer_secret_name:-}" \
PLAN_AUTHORIZER_SECRET_VERSION_STATE="${authorizer_secret_version_state:-}" \
PLAN_AUTHORIZER_SECRET_VERSION="$EXPECTED_AUTHORIZER_SECRET_VERSION" \
PLAN_WEBHOOK_SECRET_STATE="$webhook_secret_state" \
PLAN_WEBHOOK_SECRET_NAME="${webhook_secret_name:-}" \
PLAN_WEBHOOK_SECRET_VERSION_STATE="${webhook_secret_version_state:-}" \
PLAN_WEBHOOK_SECRET_VERSION="${webhook_secret_name:-}" \
python3 - "$PLAN_PATH" <<'PY'
import hashlib, json, os, sys
from pathlib import Path

repos = []
repository_ids = []
repository_remote_uris = []
for raw in Path(os.environ["PLAN_REPO_ROWS_FILE"]).read_text().splitlines():
    repo_id, owner, name, full_name, fork, remote_uri = raw.split("\t")
    repository_ids.append(int(repo_id))
    repository_remote_uris.append(remote_uri)
    repos.append({"id": int(repo_id), "owner": owner, "name": name, "full_name": full_name, "fork": fork == "true", "remote_uri": remote_uri})

repository_resources = {}
for raw in Path(os.environ["PLAN_REPOSITORY_RESOURCE_ROWS_FILE"]).read_text().splitlines():
    name, status = raw.split("\t")
    repository_resources[name] = {"status": status}

repository_hooks = {}
for raw in Path(os.environ["PLAN_HOOK_ROWS_FILE"]).read_text().splitlines():
    name, status, hook_id, hook_active, hook_events, hook_ssl = raw.split("\t")
    item = {"status": status}
    if status != "absent":
        item.update({"id": int(hook_id), "active": hook_active == "true", "events": hook_events.split(",") if hook_events else [], "insecure_ssl": hook_ssl})
    repository_hooks[name] = item

selected_repository_ids = []
selected_ids_path = Path(os.environ["PLAN_SELECTED_IDS_FILE"])
if selected_ids_path.exists():
    selected_repository_ids = [int(line) for line in selected_ids_path.read_text().splitlines() if line]

if os.environ["PLAN_INSTALLATION_STATUS"] == "exact":
    github_installation = {
        "id": int(os.environ["PLAN_INSTALLATION_ID"]),
        "account_login": os.environ["PLAN_INSTALLATION_ACCOUNT_LOGIN"],
        "repository_selection": os.environ["PLAN_INSTALLATION_REPOSITORY_SELECTION"],
        "target_type": os.environ["PLAN_INSTALLATION_TARGET_TYPE"],
        "permissions": json.loads(os.environ["PLAN_INSTALLATION_PERMISSIONS_JSON"]),
        "selected_repository_ids": selected_repository_ids,
    }
else:
    github_installation = {"status": os.environ["PLAN_INSTALLATION_STATUS"] or "missing"}

if os.environ["PLAN_AUTHORIZER_SECRET_STATE"] == "exists":
    authorizer_secret = {"name": os.environ["PLAN_AUTHORIZER_SECRET_NAME"], "state": os.environ["PLAN_AUTHORIZER_SECRET_VERSION_STATE"]}
else:
    authorizer_secret = {"status": "missing"}

if os.environ["PLAN_WEBHOOK_SECRET_STATE"] == "exists":
    webhook_secret = {"name": os.environ["PLAN_WEBHOOK_SECRET_NAME"], "state": os.environ["PLAN_WEBHOOK_SECRET_VERSION_STATE"]}
else:
    webhook_secret = {"status": "missing"}

connection = {"status": os.environ["PLAN_CONNECTION_STATE"]}
if os.environ["PLAN_CONNECTION_STATE"] == "exists":
    connection["app_installation_id"] = int(os.environ["PLAN_CONNECTION_INSTALLATION_ID"])
    connection["authorizer_token_secret_version"] = os.environ["PLAN_CONNECTION_AUTHORIZER_SECRET_VERSION"]

initial_state = {
    "repositories": repos,
    "github_installation": github_installation,
    "authorizer_token_secret_version": authorizer_secret,
    "webhook_secret_version": webhook_secret,
    "connection": connection,
    "repository_resources": repository_resources,
    "repository_hooks": repository_hooks,
}
discovery_hash = hashlib.sha256(json.dumps(initial_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

blockers = []
if os.environ["PLAN_INSTALLATION_STATUS"] != "exact":
    blockers.append({"code": "interactive-app-install-required", "message": "GitHub App installation requires an administrator browser step with the exact selected repositories and read-only permissions."})
if os.environ["PLAN_AUTHORIZER_SECRET_STATE"] != "exists":
    blockers.append({"code": "missing-github-authorizer-token-secret", "message": "Cloud Build GitHub authorizer token secret version metadata is required before a non-interactive connection can be created."})
if os.environ["PLAN_WEBHOOK_SECRET_STATE"] != "exists":
    blockers.append({"code": "missing-github-webhook-secret-version", "message": "GitHub runtime webhook secret must have an exact enabled numeric secret version."})
if os.environ["PLAN_CONNECTION_STATE"] == "exists":
    if str(os.environ["PLAN_CONNECTION_INSTALLATION_ID"]) != str(os.environ["PLAN_INSTALLATION_ID"]) or os.environ["PLAN_CONNECTION_AUTHORIZER_SECRET_VERSION"] != os.environ["PLAN_AUTHORIZER_SECRET_VERSION"]:
        blockers.append({"code": "connection-config-drift", "message": "Existing Cloud Build GitHub connection does not match the reviewed installation or authorizer secret version."})

actions = []
if os.environ["PLAN_CONNECTION_STATE"] == "missing" and os.environ["PLAN_INSTALLATION_STATUS"] == "exact" and os.environ["PLAN_AUTHORIZER_SECRET_STATE"] == "exists":
    actions.append({"kind": "create_connection", "name": os.environ["PLAN_CONNECTION_NAME"], "authorizer_token_secret_version": os.environ["PLAN_AUTHORIZER_SECRET_VERSION"], "app_installation_id": int(os.environ["PLAN_INSTALLATION_ID"])})
for repo in repos:
    if repository_resources[repo["name"]]["status"] == "missing":
        actions.append({"kind": "create_repository_resource", "name": repo["name"], "remote_uri": repo["remote_uri"]})
    actions.append({"kind": "configure_webhook", "repository_id": repo["id"], "owner": repo["owner"], "name": repo["name"], "webhook_url": os.environ["PLAN_GITHUB_WEBHOOK_URL"]})

plan = {
    "version": "mim-github-connection-plan-v4",
    "generated_at_epoch": int(os.environ["PLAN_GENERATED_AT"]),
    "expires_at_epoch": int(os.environ["PLAN_EXPIRES_AT"]),
    "status": "blocked" if blockers else "ready",
    "blockers": blockers,
    "config": {"operator_email": os.environ["PLAN_OPERATOR_EMAIL"], "project_id": os.environ["PLAN_PROJECT_ID"], "organization_id": os.environ["PLAN_ORGANIZATION_ID"], "billing_account_id": os.environ["PLAN_BILLING_ACCOUNT_ID"], "config_fingerprint": os.environ["PLAN_CONFIG_FINGERPRINT"]},
    "targets": {"connection_name": os.environ["PLAN_CONNECTION_NAME"], "region": os.environ["PLAN_FIXED_REGION"], "webhook_url": os.environ["PLAN_GITHUB_WEBHOOK_URL"], "repository_ids": repository_ids, "repository_remote_uris": repository_remote_uris, "release_identity": f"mim-release@{os.environ['PLAN_PROJECT_ID']}.iam.gserviceaccount.com"},
    "initial_state": initial_state,
    "constraints": {"owner": "madupmarketing", "non_fork_only": True, "platform_repository_forbidden": True, "repository_selection": "selected", "read_only_source_permissions": {"contents": "read", "metadata": "read"}, "operator_mutation_surface_required": True},
    "runtime_secret_contract": {"secret_name": "mim-github-webhook", "secret_version": os.environ["PLAN_WEBHOOK_SECRET_VERSION"], "apply_env": "MIM_TASK18_GITHUB_WEBHOOK_SECRET", "minimum_length_bytes": 32},
    "actions": actions,
    "required_secrets": ["mim-github-authorizer-token", "mim-github-webhook"],
    "required_apis": [],
    "discovery_hash": discovery_hash,
}
Path(sys.argv[1]).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
PY

mim_task18_write_plan_json "$PLAN_PATH" "$PLAN_OUT"
printf 'Wrote reviewed plan to %s\n' "$PLAN_OUT"
