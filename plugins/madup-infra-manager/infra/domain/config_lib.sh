#!/usr/bin/env bash

# Trusted parser for the operator-only MIM bootstrap boundary file. Callers
# must never source config.env: its contents are data, not shell code.

readonly MIM_FIXED_REGION=asia-northeast3
readonly MIM_FIXED_HOSTNAME=mim.madup.app
readonly MIM_FIXED_APEX_ACTION=leave-unconfigured

mim_config_fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

mim_is_allowed_config_key() {
  case "$1" in
    MIM_OPERATOR_EMAIL|MIM_PROJECT_ID|MIM_ORGANIZATION_ID|MIM_BILLING_ACCOUNT_ID)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

mim_is_deprecated_config_key() {
  case "$1" in
    MIM_ACCOUNT|MIM_REGION|MIM_HOSTNAME|MIM_APEX_ACTION|MIM_INITIAL_IAP_MEMBER)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

mim_is_placeholder_value() {
  local value=$1
  local lowered_value
  lowered_value=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
  [[ "$value" == *'<'* || "$value" == *'>'* || \
    "$lowered_value" == *example* || \
    "$lowered_value" == *placeholder* || \
    "$lowered_value" == *replace* || \
    "$lowered_value" == *changeme* ]]
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

# These checks protect the operator-only boundary files from accidental public
# exposure and unsafe path shapes. Same-user post-validation races are handled
# by apply-time snapshots rather than ownership semantics alone.
mim_assert_private_regular_file() {
  local label=$1
  local path=$2
  local current_uid
  local owner_uid
  local mode

  [[ -e "$path" ]] || mim_config_fail "$label is missing or unreadable"
  [[ ! -L "$path" ]] || mim_config_fail "$label must not be a symlink"
  [[ -f "$path" ]] || mim_config_fail "$label must be a regular file"
  [[ -r "$path" ]] || mim_config_fail "$label is missing or unreadable"

  current_uid=$(id -u)
  owner_uid=$(mim_stat_owner_uid "$path") || mim_config_fail "Unable to inspect $label ownership"
  [[ "$owner_uid" == "$current_uid" ]] || mim_config_fail "$label must be owned by the current user"

  mode=$(mim_stat_mode "$path") || mim_config_fail "Unable to inspect $label permissions"
  [[ "$mode" == "600" ]] || mim_config_fail "$label must use mode 0600"
}

mim_is_valid_project_id() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]
}

mim_validate_config_value() {
  local key=$1
  local value=$2

  [[ -n "$value" ]] || mim_config_fail "Missing required setting: $key"
  [[ "$value" != *[[:space:]]* ]] || mim_config_fail "Invalid $key: whitespace is not allowed"
  mim_is_placeholder_value "$value" && mim_config_fail "Invalid $key: placeholder values are not allowed"

  case "$key" in
    MIM_OPERATOR_EMAIL)
      [[ "$value" =~ ^[A-Za-z0-9._%+-]+@madup\.com$ ]] || \
        mim_config_fail "Invalid $key: must be a @madup.com user email"
      ;;
    MIM_PROJECT_ID)
      mim_is_valid_project_id "$value" || mim_config_fail "Invalid $key: must be a valid GCP project ID"
      ;;
    MIM_ORGANIZATION_ID)
      [[ "$value" =~ ^[0-9]+$ ]] || mim_config_fail "Invalid $key: must be numeric"
      ;;
    MIM_BILLING_ACCOUNT_ID)
      [[ "$value" =~ ^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$ ]] || \
        mim_config_fail "Invalid $key: must be a canonical billing account ID"
      ;;
  esac
}

mim_load_config() {
  local config_file=$1
  local line_number=0
  local raw_line=
  local seen_keys='|'

  mim_assert_private_regular_file "Config file" "$config_file"

  MIM_OPERATOR_EMAIL=
  MIM_PROJECT_ID=
  MIM_ORGANIZATION_ID=
  MIM_BILLING_ACCOUNT_ID=

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    line_number=$((line_number + 1))
    raw_line=${raw_line%$'\r'}

    case "$raw_line" in
      ''|'#'*|[[:space:]]*'#'*)
        continue
        ;;
    esac

    [[ "$raw_line" =~ ^([A-Z0-9_]+)=(.*)$ ]] || mim_config_fail "Invalid config syntax on line $line_number"

    local key=${BASH_REMATCH[1]}
    local value=${BASH_REMATCH[2]}

    if mim_is_deprecated_config_key "$key"; then
      mim_config_fail "Deprecated config key: $key"
    fi
    mim_is_allowed_config_key "$key" || mim_config_fail "Unknown config key: $key"
    [[ "$seen_keys" != *"|$key|"* ]] || mim_config_fail "Duplicate config key: $key"
    mim_validate_config_value "$key" "$value"

    printf -v "$key" '%s' "$value"
    seen_keys="${seen_keys}${key}|"
  done <"$config_file"

  local required_key
  for required_key in \
    MIM_OPERATOR_EMAIL \
    MIM_PROJECT_ID \
    MIM_ORGANIZATION_ID \
    MIM_BILLING_ACCOUNT_ID; do
    [[ -n "${!required_key:-}" ]] || mim_config_fail "Missing required setting: $required_key"
  done
}

mim_derive_iap_member() {
  printf 'user:%s' "$MIM_OPERATOR_EMAIL"
}

mim_is_safe_run_service_url() {
  local url=$1
  local remainder=
  local host=
  local prefix=
  local label=
  local labels=()
  local host_length=0

  [[ "$url" == https://* ]] || return 1
  remainder=${url#https://}
  [[ -n "$remainder" ]] || return 1
  [[ "$remainder" != *@* ]] || return 1
  [[ "$remainder" != *\?* ]] || return 1
  [[ "$remainder" != *\#* ]] || return 1

  case "$remainder" in
    */)
      host=${remainder%/}
      [[ "$host" != */* ]] || return 1
      ;;
    */*)
      return 1
      ;;
    *)
      host=$remainder
      ;;
  esac

  [[ -n "$host" ]] || return 1
  [[ "$host" == "$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')" ]] || return 1
  [[ "$host" != *:* ]] || return 1
  [[ "$host" == *.run.app ]] || return 1
  host_length=${#host}
  [[ "$host_length" -le 253 ]] || return 1

  prefix=${host%.run.app}
  [[ -n "$prefix" ]] || return 1
  [[ "$prefix" != .* ]] || return 1
  [[ "$prefix" != *. ]] || return 1
  [[ "$prefix" != *..* ]] || return 1

  IFS='.' read -r -a labels <<<"$prefix"
  [[ "${#labels[@]}" -ge 1 ]] || return 1
  for label in "${labels[@]}"; do
    [[ "${#label}" -le 63 ]] || return 1
    [[ "$label" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || return 1
  done
  return 0
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

    [[ "$raw_line" != *[[:space:]]* ]] || mim_config_fail "Protected project file entry is invalid"
    mim_is_valid_project_id "$raw_line" || mim_config_fail "Protected project file entry is invalid"
    [[ "$seen_projects" != *"|$raw_line|"* ]] || mim_config_fail "Protected project file entry is duplicated"

    project_count=$((project_count + 1))
    [[ "$raw_line" != "$selected_project_id" ]] || mim_config_fail "Selected project is protected"
    seen_projects="${seen_projects}${raw_line}|"
  done <"$protected_projects_file"

  [[ "$project_count" -gt 0 ]] || mim_config_fail "Protected project file must contain at least one project"
}
