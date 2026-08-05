#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/../release/task18_lib.sh"
CONFIG_FILE="${MIM_CONFIG_FILE:-$(mim_task18_default_config_file "$SCRIPT_DIR")}"

mim_task18_require_no_args "$@"
SNAPSHOT_DIR=$(mktemp -d)
trap 'rm -rf "$SNAPSHOT_DIR"' EXIT
SNAPSHOT_CONFIG=$(mim_task18_snapshot_config "$SCRIPT_DIR" "$CONFIG_FILE" "$SNAPSHOT_DIR")
mim_task18_load_config "$SNAPSHOT_CONFIG"
command -v gh >/dev/null 2>&1 || mim_task18_fail "gh CLI is required"
command -v gcloud >/dev/null 2>&1 || mim_task18_fail "gcloud CLI is required"
ACTIVE_ACCOUNT=$(mim_task18_assert_active_gcloud_account)
CONFIGURED_PROJECT_NUMBER=$(mim_task18_gcloud_capture \
  "Unable to inspect the configured project number" \
  projects describe "$MIM_PROJECT_ID" \
  --account="$MIM_OPERATOR_EMAIL" \
  '--format=value(projectNumber)')
[[ "$CONFIGURED_PROJECT_NUMBER" =~ ^[1-9][0-9]*$ ]] || mim_task18_fail "Configured project number must be numeric"

require_exact_enabled_secret_version() {
  local label=$1 secret_name=$2 payload_file=$3
  local state

  state=$(mim_task18_gcloud_optional_output \
    "Unable to inspect enabled $label secret versions" \
    "$payload_file" \
    secrets versions list \
    --secret="$secret_name" \
    --account="$MIM_OPERATOR_EMAIL" \
    --filter=state:ENABLED \
    --project="$MIM_PROJECT_ID" \
    '--format=json')
  [[ "$state" == "exists" ]] || mim_task18_fail "$label secret metadata must resolve to exactly one enabled numeric version resource"

  if ! SECRET_VERSION_FILE="$payload_file" SECRET_NAME="$secret_name" PROJECT_ID="$MIM_PROJECT_ID" PROJECT_NUMBER="$CONFIGURED_PROJECT_NUMBER" python3 - <<'PY'
import json
import os
import re
from pathlib import Path

payload = json.loads(Path(os.environ["SECRET_VERSION_FILE"]).read_text())
if not isinstance(payload, list):
    raise SystemExit(1)

allowed_projects = {os.environ["PROJECT_ID"], os.environ["PROJECT_NUMBER"]}
expected_secret = os.environ["SECRET_NAME"]
entries = []

for item in payload:
    if not isinstance(item, dict):
        raise SystemExit(1)
    match = re.fullmatch(
        r"projects/([^/]+)/secrets/([^/]+)/versions/([1-9][0-9]*)",
        str(item.get("name", "")),
    )
    if match is None:
        raise SystemExit(1)
    project_token, secret_name, _version_number = match.groups()
    if project_token not in allowed_projects or secret_name != expected_secret:
        raise SystemExit(1)
    if item.get("state") != "ENABLED":
        raise SystemExit(1)
    entries.append(item)

if len(entries) != 1:
    raise SystemExit(1)
PY
  then
    mim_task18_fail "$label secret metadata must resolve to exactly one enabled numeric version resource"
  fi
}

IFS=',' read -r -a repo_ids <<<"$MIM_GITHUB_REPOSITORY_IDS"
[[ "${#repo_ids[@]}" -ge 1 ]] || mim_task18_fail "Invalid MIM_GITHUB_REPOSITORY_IDS"

installation_id=
installation_account_login=
installation_repository_selection=
installation_target_type=
installation_permissions_json=

for repo_id in "${repo_ids[@]}"; do
  repo_json=$(gh api "/repositories/$repo_id") || mim_task18_fail "Unable to inspect GitHub repository ID $repo_id"
  repo_row=$(REPO_JSON="$repo_json" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["REPO_JSON"])
print("\t".join([
    str(payload["id"]),
    payload["owner"]["login"],
    payload["name"],
    payload["full_name"],
    "true" if payload["fork"] else "false",
]))
PY
)
  IFS=$'\t' read -r _parsed_id owner name full_name fork <<<"$repo_row"
  [[ "$owner" == "madupmarketing" ]] || mim_task18_fail "Platform repository is forbidden"
  [[ "$full_name" != "madup-dct/claude-plugins" && "$name" != "claude-plugins" ]] || mim_task18_fail "Platform repository is forbidden"
  [[ "$fork" == "false" ]] || mim_task18_fail "Fork repositories are forbidden"

  installation_json=$(gh api "/repos/$owner/$name/installation" 2>/dev/null) || mim_task18_fail "GitHub App installation must be completed manually before preflight passes"
  installation_row=$(INSTALLATION_JSON="$installation_json" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["INSTALLATION_JSON"])
print("\t".join([
    str(payload["id"]),
    payload["account"]["login"],
    payload["repository_selection"],
    payload["target_type"],
    json.dumps(payload["permissions"], sort_keys=True, separators=(",", ":")),
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
    mim_task18_fail "GitHub App installation must stay exact across all selected repositories"
  fi
done

[[ "$installation_account_login" == "madupmarketing" ]] || mim_task18_fail "GitHub App installation must stay within madupmarketing"
[[ "$installation_repository_selection" == "selected" ]] || mim_task18_fail "GitHub App installation must target only selected repositories"
[[ "$installation_target_type" == "Organization" ]] || mim_task18_fail "GitHub App installation must stay organization-scoped"
[[ "$installation_permissions_json" == '{"contents":"read","metadata":"read"}' ]] || mim_task18_fail "GitHub App installation must stay read-only"

selected_json=$(gh api "/user/installations/$installation_id/repositories?per_page=100") || mim_task18_fail "GitHub App installation must target exactly the reviewed repositories"
selected_ids_csv=$(SELECTED_JSON="$selected_json" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["SELECTED_JSON"])
repositories = payload.get("repositories")
if not isinstance(repositories, list):
    raise SystemExit(1)
ids = sorted(repository["id"] for repository in repositories)
print(",".join(str(repo_id) for repo_id in ids))
PY
)
expected_ids_csv=$(printf '%s\n' "${repo_ids[@]}" | sort -n | paste -sd ',' -)
[[ "$selected_ids_csv" == "$expected_ids_csv" ]] || mim_task18_fail "GitHub App installation must target exactly the reviewed repositories"

require_exact_enabled_secret_version \
  "GitHub authorizer token" \
  "$MIM_TASK18_GITHUB_AUTHORIZER_SECRET_NAME" \
  "$SNAPSHOT_DIR/authorizer-secret.json"

require_exact_enabled_secret_version \
  "GitHub webhook" \
  "$MIM_TASK18_GITHUB_WEBHOOK_SECRET_NAME" \
  "$SNAPSHOT_DIR/webhook-secret.json"

printf 'GitHub preflight checks passed.\n'
