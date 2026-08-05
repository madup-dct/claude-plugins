#!/usr/bin/env bash

readonly MIM_TASK18_PLAN_MAX_AGE_SECONDS=1800
readonly MIM_TASK18_HOSTNAME=mim.madup.app
readonly MIM_TASK18_ZONE_NAME=madup.app
readonly MIM_TASK18_ZONE_AUTHORITATIVE_NAMESERVERS=mina.ns.cloudflare.com,pete.ns.cloudflare.com
readonly MIM_TASK18_APP_HOST_SUFFIX=madup.app
readonly MIM_TASK18_GOOGLE_IDP_PROVIDER=google-workspace
readonly MIM_TASK18_ALLOWED_GROUP_EMAIL=mim-users@madup.com
readonly MIM_TASK18_GITHUB_CONNECTION_NAME=mim-github-source
readonly MIM_TASK18_GITHUB_WEBHOOK_URL=https://mim.madup.app/v1/webhooks/github
readonly MIM_TASK18_RELEASE_SOURCE_REPOSITORY=madup-dct/claude-plugins
readonly MIM_TASK18_FIXED_REGION=asia-northeast3
readonly MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_NAME=mim-runtime-bootstrap
readonly MIM_TASK18_BUILDER_IMAGE_NAME=mim-builder
readonly MIM_TASK18_RUNTIME_IMAGE_NAME=runtime
readonly MIM_TASK18_APP_GATEWAY_IMAGE_NAME=app-gateway
readonly MIM_TASK18_APP_GATEWAY_SERVICE_NAME=mim-app-gateway
readonly MIM_TASK18_APP_GATEWAY_SERVICE_ACCOUNT=mim-app-gateway
readonly MIM_TASK18_APP_GATEWAY_PROOF_SECRET_NAME=mim-app-gateway-origin-v1
readonly MIM_TASK18_GITHUB_AUTHORIZER_SECRET_NAME=mim-github-authorizer-token
readonly MIM_TASK18_GITHUB_WEBHOOK_SECRET_NAME=mim-github-webhook
readonly MIM_TASK18_SLACK_REDIRECT_URI=https://mim.madup.app/slack/oauth/callback
readonly MIM_TASK18_SLACK_REQUIRED_SCOPES=chat:write,commands
readonly MIM_TASK18_SLACK_TENANT_EVIDENCE_VERSION=mim-slack-tenant-evidence-v1

mim_task18_fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

mim_task18_stat_mode() {
  local path=$1
  if stat -f '%Lp' "$path" >/dev/null 2>&1; then
    stat -f '%Lp' "$path"
    return
  fi
  if stat -c '%a' "$path" >/dev/null 2>&1; then
    stat -c '%a' "$path"
    return
  fi
  return 1
}

mim_task18_stat_owner_uid() {
  local path=$1
  if stat -f '%u' "$path" >/dev/null 2>&1; then
    stat -f '%u' "$path"
    return
  fi
  if stat -c '%u' "$path" >/dev/null 2>&1; then
    stat -c '%u' "$path"
    return
  fi
  return 1
}

mim_task18_assert_private_regular_file() {
  local label=$1
  local path=$2
  local owner_uid current_uid mode

  [[ -e "$path" ]] || mim_task18_fail "$label is missing or unreadable"
  [[ ! -L "$path" ]] || mim_task18_fail "$label must not be a symlink"
  [[ -f "$path" ]] || mim_task18_fail "$label must be a regular file"
  [[ -r "$path" ]] || mim_task18_fail "$label is missing or unreadable"

  current_uid=$(id -u)
  owner_uid=$(mim_task18_stat_owner_uid "$path") || mim_task18_fail "Unable to inspect $label ownership"
  [[ "$owner_uid" == "$current_uid" ]] || mim_task18_fail "$label must be owned by the current user"

  mode=$(mim_task18_stat_mode "$path") || mim_task18_fail "Unable to inspect $label permissions"
  [[ "$mode" == "600" ]] || mim_task18_fail "$label must use mode 0600"
}

mim_task18_is_placeholder_value() {
  local value=$1
  local lowered
  lowered=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
  [[ "$value" == *'<'* || "$value" == *'>'* || \
    "$lowered" == *example* || \
    "$lowered" == *required* || \
    "$lowered" == *placeholder* || \
    "$lowered" == *replace* || \
    "$lowered" == *changeme* ]]
}

mim_task18_is_allowed_config_key() {
  case "$1" in
    MIM_OPERATOR_EMAIL|MIM_PROJECT_ID|MIM_ORGANIZATION_ID|MIM_BILLING_ACCOUNT_ID|\
    MIM_CLOUDFLARE_ACCOUNT_ID|MIM_CLOUDFLARE_ZONE_ID|MIM_CLOUDFLARE_TEAM_NAME|\
    MIM_GITHUB_REPOSITORY_IDS|MIM_SLACK_ENABLED|MIM_SLACK_APP_ID|MIM_SLACK_APPROVED_ORG_ID|\
    MIM_SLACK_APPROVED_WORKSPACE_IDS)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

mim_task18_is_valid_project_id() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]
}

mim_task18_validate_csv_regex() {
  local value=$1
  local pattern=$2
  local item
  IFS=',' read -r -a items <<<"$value"
  [[ "${#items[@]}" -ge 1 ]] || return 1
  for item in "${items[@]}"; do
    [[ -n "$item" ]] || return 1
    [[ "$item" =~ $pattern ]] || return 1
  done
  return 0
}

mim_task18_validate_config_value() {
  local key=$1
  local value=$2
  [[ -n "$value" ]] || mim_task18_fail "Missing required setting: $key"
  [[ "$value" != *[[:space:]]* ]] || mim_task18_fail "Invalid $key: whitespace is not allowed"
  mim_task18_is_placeholder_value "$value" && mim_task18_fail "Invalid $key: placeholder values are not allowed"

  case "$key" in
    MIM_OPERATOR_EMAIL)
      [[ "$value" =~ ^[A-Za-z0-9._%+-]+@madup\.com$ ]] || mim_task18_fail "Invalid $key: must be a @madup.com email"
      ;;
    MIM_PROJECT_ID)
      mim_task18_is_valid_project_id "$value" || mim_task18_fail "Invalid $key: must be a valid GCP project ID"
      ;;
    MIM_ORGANIZATION_ID)
      [[ "$value" =~ ^[0-9]+$ ]] || mim_task18_fail "Invalid $key: must be numeric"
      ;;
    MIM_BILLING_ACCOUNT_ID)
      [[ "$value" =~ ^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$ ]] || mim_task18_fail "Invalid $key: must be a canonical billing account ID"
      ;;
    MIM_CLOUDFLARE_ACCOUNT_ID)
      [[ "$value" =~ ^[A-Za-z0-9_-]{6,64}$ ]] || mim_task18_fail "Invalid $key"
      ;;
    MIM_CLOUDFLARE_ZONE_ID)
      [[ "$value" =~ ^[A-Za-z0-9]{8,64}$ ]] || mim_task18_fail "Invalid $key"
      ;;
    MIM_CLOUDFLARE_TEAM_NAME)
      [[ "$value" =~ ^[a-z0-9-]{3,63}$ ]] || mim_task18_fail "Invalid $key"
      ;;
    MIM_GITHUB_REPOSITORY_IDS)
      mim_task18_validate_csv_regex "$value" '^[0-9]+$' || mim_task18_fail "Invalid $key"
      ;;
    MIM_SLACK_ENABLED)
      [[ "$value" == "true" || "$value" == "false" ]] || mim_task18_fail "Invalid $key: must be exactly true or false"
      ;;
    MIM_SLACK_APP_ID)
      [[ "$value" =~ ^[A-Z0-9]{6,32}$ ]] || mim_task18_fail "Invalid $key"
      ;;
    MIM_SLACK_APPROVED_ORG_ID)
      [[ "$value" == none || "$value" =~ ^E[A-Z0-9]{8,}$ ]] || mim_task18_fail "Invalid $key"
      ;;
    MIM_SLACK_APPROVED_WORKSPACE_IDS)
      mim_task18_validate_csv_regex "$value" '^T[A-Z0-9]{8,}$' || mim_task18_fail "Invalid $key"
      ;;
  esac
}

mim_task18_load_config() {
  local config_file=$1
  local line line_number seen required_key
  line_number=0
  seen='|'

  mim_task18_assert_private_regular_file "Config file" "$config_file"

  MIM_OPERATOR_EMAIL=
  MIM_PROJECT_ID=
  MIM_ORGANIZATION_ID=
  MIM_BILLING_ACCOUNT_ID=
  MIM_CLOUDFLARE_ACCOUNT_ID=
  MIM_CLOUDFLARE_ZONE_ID=
  MIM_CLOUDFLARE_TEAM_NAME=
  MIM_GITHUB_REPOSITORY_IDS=
  MIM_SLACK_ENABLED=false
  MIM_SLACK_APP_ID=
  MIM_SLACK_APPROVED_ORG_ID=
  MIM_SLACK_APPROVED_WORKSPACE_IDS=

  while IFS= read -r line || [[ -n "$line" ]]; do
    line_number=$((line_number + 1))
    line=${line%$'\r'}
    case "$line" in
      ''|'#'*|[[:space:]]*'#'*)
        continue
        ;;
    esac
    [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]] || mim_task18_fail "Invalid config syntax on line $line_number"
    local key=${BASH_REMATCH[1]}
    local value=${BASH_REMATCH[2]}
    mim_task18_is_allowed_config_key "$key" || mim_task18_fail "Unknown config key: $key"
    [[ "$seen" != *"|$key|"* ]] || mim_task18_fail "Duplicate config key: $key"
    mim_task18_validate_config_value "$key" "$value"
    printf -v "$key" '%s' "$value"
    seen="${seen}${key}|"
  done <"$config_file"

  for required_key in \
    MIM_OPERATOR_EMAIL MIM_PROJECT_ID MIM_ORGANIZATION_ID MIM_BILLING_ACCOUNT_ID \
    MIM_CLOUDFLARE_ACCOUNT_ID MIM_CLOUDFLARE_ZONE_ID MIM_CLOUDFLARE_TEAM_NAME \
    MIM_GITHUB_REPOSITORY_IDS; do
    [[ -n "${!required_key:-}" ]] || mim_task18_fail "Missing required setting: $required_key"
  done

  if [[ "$MIM_SLACK_ENABLED" == "true" ]]; then
    for required_key in \
      MIM_SLACK_APP_ID MIM_SLACK_APPROVED_ORG_ID \
      MIM_SLACK_APPROVED_WORKSPACE_IDS; do
      [[ -n "${!required_key:-}" ]] || mim_task18_fail "Missing required setting: $required_key"
    done
  fi
}

mim_task18_require_no_args() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --*) mim_task18_fail "Unknown argument: $arg" ;;
      *) mim_task18_fail "Positional arguments are not supported" ;;
    esac
  done
}

mim_task18_default_config_file() {
  local script_dir=$1
  printf '%s/config.env' "$script_dir"
}

mim_task18_default_state_dir() {
  local script_dir=$1
  printf '%s/.state' "$script_dir"
}

mim_task18_default_tenant_evidence_file() {
  local script_dir=$1
  printf '%s/slack-tenant-evidence.json' "$(mim_task18_default_state_dir "$script_dir")"
}

mim_task18_default_protected_projects_file() {
  local script_dir=$1
  printf '%s/protected-projects.exact' "$script_dir"
}

mim_task18_plugin_root() {
  local script_dir=$1
  (cd "$script_dir/../.." && pwd -P)
}

mim_task18_repo_root() {
  local script_dir=$1
  (cd "$script_dir/../../../.." && pwd -P)
}

mim_task18_snapshot_helper() {
  local script_dir=$1
  printf '%s/../domain/snapshot_private_files.py' "$script_dir"
}

mim_task18_ensure_state_dir() {
  local state_dir=$1
  [[ ! -L "$state_dir" ]] || mim_task18_fail ".state directory must not be a symlink"
  mkdir -p "$state_dir"
  chmod 700 "$state_dir"
}

mim_task18_resolve_physical_dir() {
  local dir=$1
  (cd "$dir" && pwd -P)
}

mim_task18_safe_plan_filename() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9._-]*\.json$ ]]
}

mim_task18_assert_plan_create_path() {
  local script_dir=$1
  local path=$2
  local state_dir expected_parent actual_parent base_name

  state_dir=$(mim_task18_default_state_dir "$script_dir")
  mim_task18_ensure_state_dir "$state_dir"
  base_name=$(basename "$path")
  mim_task18_safe_plan_filename "$base_name" || mim_task18_fail "Plan filename is invalid"
  expected_parent=$(mim_task18_resolve_physical_dir "$state_dir")
  actual_parent=$(mim_task18_resolve_physical_dir "$(dirname "$path")" 2>/dev/null || true)
  [[ "$actual_parent" == "$expected_parent" ]] || mim_task18_fail "Plan output must stay inside the literal .state directory"
  [[ ! -L "$path" ]] || mim_task18_fail "Plan output target must not be a symlink"
  [[ ! -e "$path" ]] || mim_task18_fail "Refusing to overwrite existing reviewed plan"
  [[ ! -e "$path.sha256" ]] || mim_task18_fail "Refusing to overwrite existing reviewed plan"
}

mim_task18_assert_plan_read_path() {
  local script_dir=$1
  local path=$2
  local state_dir expected_parent actual_parent

  state_dir=$(mim_task18_default_state_dir "$script_dir")
  mim_task18_ensure_state_dir "$state_dir"
  expected_parent=$(mim_task18_resolve_physical_dir "$state_dir")
  actual_parent=$(mim_task18_resolve_physical_dir "$(dirname "$path")" 2>/dev/null || true)
  [[ "$actual_parent" == "$expected_parent" ]] || mim_task18_fail "Plan file must stay inside the literal .state directory"
  mim_task18_assert_private_regular_file "Plan file" "$path"
  mim_task18_assert_private_regular_file "Plan hash file" "$path.sha256"
}

mim_task18_sha256_file() {
  local path=$1
  if command -v shasum >/dev/null 2>&1; then
    LC_ALL=C shasum -a 256 "$path" | awk '{print $1}'
    return
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return
  fi
  mim_task18_fail "A SHA-256 tool is required"
}

mim_task18_now_epoch() {
  date +%s
}

mim_task18_write_hash_sidecar() {
  local plan_path=$1
  printf '%s  %s\n' "$(mim_task18_sha256_file "$plan_path")" "$(basename "$plan_path")" >"$plan_path.sha256"
  chmod 600 "$plan_path.sha256"
}

mim_task18_snapshot_config() {
  local script_dir=$1
  local config_file=$2
  local snapshot_dir=$3
  local helper snapshot_file
  helper=$(mim_task18_snapshot_helper "$script_dir")
  [[ -f "$helper" ]] || mim_task18_fail "Snapshot helper is required"
  mkdir -p "$snapshot_dir"
  chmod 700 "$snapshot_dir"
  snapshot_file="$snapshot_dir/config.env"
  python3 "$helper" --snapshot "Config file" "$config_file" "$snapshot_file" 65536
  printf '%s' "$snapshot_file"
}

mim_task18_write_plan_json() {
  local temp_plan=$1
  local out_path=$2
  cp "$temp_plan" "$out_path"
  chmod 600 "$out_path"
  mim_task18_write_hash_sidecar "$out_path"
}

mim_task18_validate_plan_hash_and_age() {
  local plan_path=$1
  local expected_hash actual_hash generated_at expires_at now_epoch

  expected_hash=$(awk '{print $1}' "$plan_path.sha256")
  actual_hash=$(mim_task18_sha256_file "$plan_path")
  [[ "$expected_hash" == "$actual_hash" ]] || mim_task18_fail "Plan hash verification failed"

  generated_at=$(python3 - "$plan_path" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["generated_at_epoch"])
PY
)
  expires_at=$(python3 - "$plan_path" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data["expires_at_epoch"])
PY
)
  [[ "$generated_at" =~ ^[0-9]+$ ]] || mim_task18_fail "Plan file does not match the expected reviewed contract"
  [[ "$expires_at" =~ ^[0-9]+$ ]] || mim_task18_fail "Plan file does not match the expected reviewed contract"
  now_epoch=$(mim_task18_now_epoch)
  (( generated_at <= now_epoch )) || mim_task18_fail "Plan generated_at cannot be in the future"
  (( expires_at - generated_at == MIM_TASK18_PLAN_MAX_AGE_SECONDS )) || mim_task18_fail "Plan expiry must be exactly 1800 seconds after generation"
  (( now_epoch <= expires_at )) || mim_task18_fail "Plan is older than 30 minutes"
}

mim_task18_compare_plans() {
  local provided_plan=$1
  local expected_plan=$2
  python3 - "$provided_plan" "$expected_plan" <<'PY'
import json, sys
from pathlib import Path
provided = json.loads(Path(sys.argv[1]).read_text())
expected = json.loads(Path(sys.argv[2]).read_text())
if provided.get("discovery_hash") != expected.get("discovery_hash"):
    print("drift")
elif provided != expected:
    print("mismatch")
else:
    print("ok")
PY
}

mim_task18_config_fingerprint() {
  local snapshot_config=$1
  mim_task18_sha256_file "$snapshot_config"
}

mim_task18_release_identity_email() {
  printf 'mim-release@%s.iam.gserviceaccount.com' "$MIM_PROJECT_ID"
}

mim_task18_assert_active_gcloud_account() {
  local active_account
  active_account=$(mim_task18_gcloud_capture \
    "Unable to determine the active gcloud account" \
    auth list \
    --filter=status:ACTIVE \
    '--format=value(account)' \
    --account="$MIM_OPERATOR_EMAIL")
  [[ "$active_account" == "$MIM_OPERATOR_EMAIL" ]] || mim_task18_fail "Active gcloud account does not match the configured operator"
  printf '%s' "$active_account"
}

mim_task18_build_service_account_email() {
  printf 'mim-build@%s.iam.gserviceaccount.com' "$MIM_PROJECT_ID"
}

mim_task18_build_service_account_resource() {
  printf 'projects/%s/serviceAccounts/%s' "$MIM_PROJECT_ID" "$(mim_task18_build_service_account_email)"
}

mim_task18_exact_secret_version_ref() {
  local secret_name=$1
  local version_number=$2
  [[ "$version_number" =~ ^[1-9][0-9]*$ ]] || mim_task18_fail "Secret version must be numeric"
  printf 'projects/%s/secrets/%s/versions/%s' "$MIM_PROJECT_ID" "$secret_name" "$version_number"
}

mim_task18_github_authorizer_secret_version() {
  local version_number=$1
  mim_task18_exact_secret_version_ref "$MIM_TASK18_GITHUB_AUTHORIZER_SECRET_NAME" "$version_number"
}

mim_task18_github_webhook_secret_version() {
  local version_number=$1
  mim_task18_exact_secret_version_ref "$MIM_TASK18_GITHUB_WEBHOOK_SECRET_NAME" "$version_number"
}

mim_task18_require_exact_true_env() {
  local name=$1
  local value=${!name-}
  [[ "$value" == "true" ]] || mim_task18_fail "$name must be exactly true"
}

mim_task18_require_secret_length() {
  local label=$1
  local value=$2
  local min_bytes=$3
  [[ "${#value}" -ge "$min_bytes" ]] || mim_task18_fail "$label must contain at least $min_bytes bytes"
}

mim_task18_assert_state_json_read_path() {
  local script_dir=$1
  local path=$2
  local state_dir expected_parent actual_parent

  state_dir=$(mim_task18_default_state_dir "$script_dir")
  mim_task18_ensure_state_dir "$state_dir"
  expected_parent=$(mim_task18_resolve_physical_dir "$state_dir")
  actual_parent=$(mim_task18_resolve_physical_dir "$(dirname "$path")" 2>/dev/null || true)
  [[ "$actual_parent" == "$expected_parent" ]] || mim_task18_fail "State evidence file must stay inside the literal .state directory"
  mim_task18_assert_private_regular_file "State evidence file" "$path"
  mim_task18_assert_private_regular_file "State evidence hash file" "$path.sha256"
}

mim_task18_validate_digest_image_uri() {
  local value=$1
  local repository=$2
  [[ "$value" =~ ^${MIM_TASK18_FIXED_REGION}-docker\.pkg\.dev/${MIM_PROJECT_ID}/${repository}/[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]]
}

mim_task18_expected_tagged_image_ref() {
  local repository=$1
  local image_name=$2
  local source_commit=$3
  printf '%s-docker.pkg.dev/%s/%s/%s:git-%s' \
    "$MIM_TASK18_FIXED_REGION" "$MIM_PROJECT_ID" "$repository" "$image_name" "$source_commit"
}

mim_task18_validate_bootstrap_secret_version_ref() {
  local value=$1
  [[ "$value" =~ ^projects/${MIM_PROJECT_ID}/secrets/${MIM_TASK18_RUNTIME_BOOTSTRAP_SECRET_NAME}/versions/[1-9][0-9]*$ ]]
}

mim_task18_validate_secret_version_ref() {
  local value=$1
  local secret_name=$2
  [[ "$value" =~ ^projects/${MIM_PROJECT_ID}/secrets/${secret_name}/versions/[1-9][0-9]*$ ]]
}

mim_task18_validate_key_id() {
  local value=$1
  [[ "$value" =~ ^[A-Za-z0-9._-]{1,128}$ ]]
}

mim_task18_validate_cloudflare_audience() {
  local value=$1
  [[ "$value" =~ ^[A-Za-z0-9._-]{8,200}$ ]]
}

mim_task18_validate_https_origin() {
  local value=$1
  python3 - "$value" <<'PY' >/dev/null
import sys
from urllib.parse import urlparse

parsed = urlparse(sys.argv[1])
if parsed.scheme != "https" or not parsed.hostname or parsed.port or parsed.username or parsed.password:
    raise SystemExit(1)
if parsed.params or parsed.query or parsed.fragment:
    raise SystemExit(1)
if parsed.path not in ("", "/"):
    raise SystemExit(1)
PY
}

mim_task18_bootstrap_secret_version_number() {
  local value=$1
  printf '%s' "${value##*/}"
}

mim_task18_secret_version_number() {
  local value=$1
  printf '%s' "${value##*/}"
}

mim_task18_secret_name_from_ref() {
  local value=$1
  local trimmed=${value#projects/${MIM_PROJECT_ID}/secrets/}
  printf '%s' "${trimmed%%/versions/*}"
}

mim_task18_gcloud_capture() {
  local description=$1
  shift
  local output
  if ! output=$(gcloud "$@" 2>/dev/null); then
    mim_task18_fail "$description"
  fi
  printf '%s' "$output"
}

mim_task18_gcloud_optional_output() {
  local description=$1
  local output_file=$2
  shift 2

  local stderr_file status
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

  if grep -Eq 'NOT_FOUND:|Cannot find service|not found|Resource not found' "$stderr_file"; then
    rm -f -- "$stderr_file"
    : >"$output_file"
    printf 'missing'
    return
  fi

  rm -f -- "$stderr_file"
  mim_task18_fail "$description"
}
