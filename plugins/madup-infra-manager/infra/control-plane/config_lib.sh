#!/usr/bin/env bash

readonly MIM_FIXED_REGION=asia-northeast3
readonly MIM_PLAN_VERSION=mim-control-plane-plan-v2
readonly MIM_PLAN_MAX_AGE_SECONDS=1800
readonly MIM_CONTROL_PLANE_SERVICE=mim-control-plane
readonly MIM_ARTIFACT_REPOSITORY=mim-control-plane
readonly MIM_PRIVATE_QUEUE=mim-private-workers
readonly MIM_FIRESTORE_DATABASE='(default)'
readonly MIM_FIRESTORE_TYPE=FIRESTORE_NATIVE
readonly MIM_QUEUE_STATE=RUNNING
readonly MIM_QUEUE_MAX_ATTEMPTS=4
readonly MIM_QUEUE_MAX_RETRY_DURATION=300s
readonly MIM_QUEUE_MIN_BACKOFF=5s
readonly MIM_QUEUE_MAX_BACKOFF=60s
readonly MIM_QUEUE_MAX_DOUBLINGS=3

mim_fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

mim_stat_mode() {
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

mim_stat_owner_uid() {
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

mim_assert_private_regular_file() {
  local label=$1
  local path=$2
  local owner_uid
  local current_uid
  local mode

  [[ -e "$path" ]] || mim_fail "$label is missing or unreadable"
  [[ ! -L "$path" ]] || mim_fail "$label must not be a symlink"
  [[ -f "$path" ]] || mim_fail "$label must be a regular file"
  [[ -r "$path" ]] || mim_fail "$label is missing or unreadable"

  current_uid=$(id -u)
  owner_uid=$(mim_stat_owner_uid "$path") || mim_fail "Unable to inspect $label ownership"
  [[ "$owner_uid" == "$current_uid" ]] || mim_fail "$label must be owned by the current user"

  mode=$(mim_stat_mode "$path") || mim_fail "Unable to inspect $label permissions"
  [[ "$mode" == "600" ]] || mim_fail "$label must use mode 0600"
}

mim_is_placeholder_value() {
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

mim_is_allowed_config_key() {
  case "$1" in
    MIM_OPERATOR_EMAIL|MIM_PROJECT_ID|MIM_ORGANIZATION_ID|MIM_BILLING_ACCOUNT_ID|\
    MIM_CLOUDFLARE_ACCOUNT_ID|MIM_CLOUDFLARE_ZONE_ID|MIM_CLOUDFLARE_TEAM_NAME|\
    MIM_GITHUB_REPOSITORY_IDS|MIM_SLACK_APP_ID|MIM_SLACK_APPROVED_ORG_ID|\
    MIM_SLACK_APPROVED_WORKSPACE_IDS)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

mim_is_valid_project_id() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]
}

mim_validate_csv_regex() {
  local value=$1
  local pattern=$2
  local item=

  IFS=',' read -r -a items <<<"$value"
  [[ "${#items[@]}" -ge 1 ]] || return 1
  for item in "${items[@]}"; do
    [[ -n "$item" ]] || return 1
    [[ "$item" =~ $pattern ]] || return 1
  done
  return 0
}

mim_validate_config_value() {
  local key=$1
  local value=$2

  [[ -n "$value" ]] || mim_fail "Missing required setting: $key"
  [[ "$value" != *[[:space:]]* ]] || mim_fail "Invalid $key: whitespace is not allowed"
  mim_is_placeholder_value "$value" && mim_fail "Invalid $key: placeholder values are not allowed"

  case "$key" in
    MIM_OPERATOR_EMAIL)
      [[ "$value" =~ ^[A-Za-z0-9._%+-]+@madup\.com$ ]] || mim_fail "Invalid $key: must be a @madup.com email"
      ;;
    MIM_PROJECT_ID)
      mim_is_valid_project_id "$value" || mim_fail "Invalid $key: must be a valid GCP project ID"
      ;;
    MIM_ORGANIZATION_ID)
      [[ "$value" =~ ^[0-9]+$ ]] || mim_fail "Invalid $key: must be numeric"
      ;;
    MIM_BILLING_ACCOUNT_ID)
      [[ "$value" =~ ^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$ ]] || mim_fail "Invalid $key: must be a canonical billing account ID"
      ;;
    MIM_CLOUDFLARE_ACCOUNT_ID)
      [[ "$value" =~ ^[A-Za-z0-9_-]{6,64}$ ]] || mim_fail "Invalid $key"
      ;;
    MIM_CLOUDFLARE_ZONE_ID)
      [[ "$value" =~ ^[A-Za-z0-9]{8,64}$ ]] || mim_fail "Invalid $key"
      ;;
    MIM_CLOUDFLARE_TEAM_NAME)
      [[ "$value" =~ ^[a-z0-9-]{3,63}$ ]] || mim_fail "Invalid $key"
      ;;
    MIM_GITHUB_REPOSITORY_IDS)
      mim_validate_csv_regex "$value" '^[0-9]+$' || mim_fail "Invalid $key"
      ;;
    MIM_SLACK_APP_ID)
      [[ "$value" =~ ^[A-Z0-9]{6,32}$ ]] || mim_fail "Invalid $key"
      ;;
    MIM_SLACK_APPROVED_ORG_ID)
      [[ "$value" == none || "$value" =~ ^E[A-Z0-9]{8,}$ ]] || mim_fail "Invalid $key"
      ;;
    MIM_SLACK_APPROVED_WORKSPACE_IDS)
      mim_validate_csv_regex "$value" '^T[A-Z0-9]{8,}$' || mim_fail "Invalid $key"
      ;;
  esac
}

mim_load_config() {
  local config_file=$1
  local line=
  local line_number=0
  local seen='|'
  local required_key=

  mim_assert_private_regular_file "Config file" "$config_file"

  MIM_OPERATOR_EMAIL=
  MIM_PROJECT_ID=
  MIM_ORGANIZATION_ID=
  MIM_BILLING_ACCOUNT_ID=
  MIM_CLOUDFLARE_ACCOUNT_ID=
  MIM_CLOUDFLARE_ZONE_ID=
  MIM_CLOUDFLARE_TEAM_NAME=
  MIM_GITHUB_REPOSITORY_IDS=
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

    [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]] || mim_fail "Invalid config syntax on line $line_number"

    local key=${BASH_REMATCH[1]}
    local value=${BASH_REMATCH[2]}

    mim_is_allowed_config_key "$key" || mim_fail "Unknown config key: $key"
    [[ "$seen" != *"|$key|"* ]] || mim_fail "Duplicate config key: $key"
    mim_validate_config_value "$key" "$value"
    printf -v "$key" '%s' "$value"
    seen="${seen}${key}|"
  done <"$config_file"

  for required_key in \
    MIM_OPERATOR_EMAIL \
    MIM_PROJECT_ID \
    MIM_ORGANIZATION_ID \
    MIM_BILLING_ACCOUNT_ID \
    MIM_CLOUDFLARE_ACCOUNT_ID \
    MIM_CLOUDFLARE_ZONE_ID \
    MIM_CLOUDFLARE_TEAM_NAME \
    MIM_GITHUB_REPOSITORY_IDS \
    MIM_SLACK_APP_ID \
    MIM_SLACK_APPROVED_ORG_ID \
    MIM_SLACK_APPROVED_WORKSPACE_IDS; do
    [[ -n "${!required_key:-}" ]] || mim_fail "Missing required setting: $required_key"
  done
}

mim_require_no_args() {
  local arg=
  for arg in "$@"; do
    case "$arg" in
      --*)
        mim_fail "Unknown argument: $arg"
        ;;
      *)
        mim_fail "Positional arguments are not supported"
        ;;
    esac
  done
}

mim_default_config_file() {
  local script_dir=$1
  printf '%s/config.env' "$script_dir"
}

mim_default_protected_projects_file() {
  local script_dir=$1
  printf '%s/protected-projects.exact' "$script_dir"
}

mim_default_state_dir() {
  local script_dir=$1
  printf '%s/.state' "$script_dir"
}

mim_plugin_root() {
  local script_dir=$1
  if [[ -n "${MIM_PLUGIN_ROOT:-}" ]]; then
    printf '%s' "$MIM_PLUGIN_ROOT"
    return
  fi
  (cd "$script_dir/../.." && pwd -P)
}

mim_snapshot_helper_path() {
  local script_dir=$1
  printf '%s/../domain/snapshot_private_files.py' "$script_dir"
}

mim_has_exact_line() {
  local haystack=$1
  local needle=$2
  local line=

  while IFS= read -r line; do
    [[ "$line" == "$needle" ]] && return 0
  done <<<"$haystack"

  return 1
}

mim_assert_project_not_protected() {
  local selected_project_id=$1
  local protected_projects_file=$2
  local raw_line=
  local seen_projects='|'
  local project_count=0

  mim_assert_private_regular_file "Protected project file" "$protected_projects_file"

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    raw_line=${raw_line%$'\r'}
    case "$raw_line" in
      ''|'#'*|[[:space:]]*'#'*)
        continue
        ;;
    esac

    [[ "$raw_line" != *[[:space:]]* ]] || mim_fail "Protected project file entry is invalid"
    mim_is_valid_project_id "$raw_line" || mim_fail "Protected project file entry is invalid"
    [[ "$seen_projects" != *"|$raw_line|"* ]] || mim_fail "Protected project file entry is duplicated"
    [[ "$raw_line" != "$selected_project_id" ]] || mim_fail "Selected project is protected"

    seen_projects="${seen_projects}${raw_line}|"
    project_count=$((project_count + 1))
  done <"$protected_projects_file"

  [[ "$project_count" -gt 0 ]] || mim_fail "Protected project file must contain at least one project"
}

mim_redact_value() {
  printf '[redacted]'
}

mim_print_redacted_config_summary() {
  printf 'operator_email=%s\n' "$MIM_OPERATOR_EMAIL"
  printf 'project_id=%s\n' "$MIM_PROJECT_ID"
  printf 'organization_id=%s\n' "$MIM_ORGANIZATION_ID"
  printf 'billing_account_id=%s\n' "$MIM_BILLING_ACCOUNT_ID"
  printf 'cloudflare_account_id=%s\n' "$(mim_redact_value)"
  printf 'cloudflare_zone_id=%s\n' "$(mim_redact_value)"
  printf 'cloudflare_team_name=%s\n' "$(mim_redact_value)"
  printf 'github_repository_ids=%s\n' "$(mim_redact_value)"
  printf 'slack_app_id=%s\n' "$(mim_redact_value)"
  printf 'slack_approved_org_id=%s\n' "$(mim_redact_value)"
  printf 'slack_approved_workspace_ids=%s\n' "$(mim_redact_value)"
}

mim_required_api_list() {
  cat <<'EOF'
run.googleapis.com
cloudbuild.googleapis.com
artifactregistry.googleapis.com
cloudtasks.googleapis.com
secretmanager.googleapis.com
firestore.googleapis.com
iap.googleapis.com
iam.googleapis.com
iamcredentials.googleapis.com
cloudscheduler.googleapis.com
cloudresourcemanager.googleapis.com
serviceusage.googleapis.com
EOF
}

mim_secret_names() {
  cat <<'EOF'
mim-runtime-bootstrap
mim-edge-origin-v1
mim-app-gateway-origin-v1
mim-desired-state-signing
mim-github-webhook
mim-github-app-key
EOF
}

mim_fixed_secret_iam_rows() {
  cat <<'EOF'
mim-runtime-bootstrap	required
mim-edge-origin-v1	required
mim-app-gateway-origin-v1	required
mim-app-gateway-origin-v0	optional
mim-desired-state-signing	required
mim-github-webhook	required
mim-github-app-key	required
EOF
}

mim_managed_identity_rows() {
  cat <<'EOF'
control_plane	mim-control-plane
app_gateway	mim-app-gateway
deploy_worker	mim-deploy-worker
build	mim-build
schedule_gateway	mim-schedule-gateway
maintenance	mim-maintenance
identity_sync	mim-identity-sync
release	mim-release
EOF
}

mim_identity_email() {
  local service_account_name=$1
  printf '%s@%s.iam.gserviceaccount.com' "$service_account_name" "$MIM_PROJECT_ID"
}

mim_safe_plan_filename() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9._-]*\.json$ ]]
}

mim_ensure_state_dir() {
  local state_dir=$1

  if [[ -L "$state_dir" ]]; then
    mim_fail ".state directory must not be a symlink"
  fi

  mkdir -p "$state_dir"
  chmod 700 "$state_dir"
}

mim_resolve_physical_dir() {
  local dir=$1
  (cd "$dir" && pwd -P)
}

mim_assert_plan_create_path() {
  local script_dir=$1
  local path=$2
  local state_dir expected_parent actual_parent base_name

  state_dir=$(mim_default_state_dir "$script_dir")
  mim_ensure_state_dir "$state_dir"

  base_name=$(basename "$path")
  mim_safe_plan_filename "$base_name" || mim_fail "Plan filename is invalid"

  expected_parent=$(mim_resolve_physical_dir "$state_dir")
  actual_parent=$(mim_resolve_physical_dir "$(dirname "$path")" 2>/dev/null || true)
  [[ "$actual_parent" == "$expected_parent" ]] || mim_fail "Plan output must stay inside the literal .state directory"

  [[ ! -L "$path" ]] || mim_fail "Plan output target must not be a symlink"
  [[ ! -e "$path" ]] || mim_fail "Refusing to overwrite existing reviewed plan"
  [[ ! -e "$path.sha256" ]] || mim_fail "Refusing to overwrite existing reviewed plan"
}

mim_assert_plan_read_path() {
  local script_dir=$1
  local path=$2
  local state_dir expected_parent actual_parent

  state_dir=$(mim_default_state_dir "$script_dir")
  mim_ensure_state_dir "$state_dir"

  expected_parent=$(mim_resolve_physical_dir "$state_dir")
  actual_parent=$(mim_resolve_physical_dir "$(dirname "$path")" 2>/dev/null || true)
  [[ "$actual_parent" == "$expected_parent" ]] || mim_fail "Plan file must stay inside the literal .state directory"

  mim_assert_private_regular_file "Plan file" "$path"
  mim_assert_private_regular_file "Plan hash file" "$path.sha256"
}

mim_sha256_file() {
  local path=$1

  if command -v shasum >/dev/null 2>&1; then
    LC_ALL=C LANG=C shasum -a 256 "$path" | awk '{print $1}'
    return
  fi

  if command -v sha256sum >/dev/null 2>&1; then
    LC_ALL=C LANG=C sha256sum "$path" | awk '{print $1}'
    return
  fi

  mim_fail "A SHA-256 tool is required"
}

mim_now_epoch() {
  date +%s
}
