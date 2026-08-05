#!/usr/bin/env bash

readonly MIM_RTB_PLAN_MAX_AGE_SECONDS=1800
readonly MIM_RTB_CENTRAL_PROJECT_ID=mim-prod-123456
readonly MIM_RTB_FIXED_REGION=asia-northeast3
readonly MIM_RTB_TARGET_SECRET_NAME=mim-runtime-bootstrap
readonly MIM_RTB_RELEASE_SERVICE_ACCOUNT="mim-release@${MIM_RTB_CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"

mim_rtb_fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

mim_rtb_stat_mode() {
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

mim_rtb_stat_owner_uid() {
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

mim_rtb_assert_private_regular_file() {
  local label=$1
  local path=$2
  local owner_uid current_uid mode

  [[ -e "$path" ]] || mim_rtb_fail "$label is missing or unreadable"
  [[ ! -L "$path" ]] || mim_rtb_fail "$label must not be a symlink"
  [[ -f "$path" ]] || mim_rtb_fail "$label must be a regular file"
  [[ -r "$path" ]] || mim_rtb_fail "$label is missing or unreadable"

  current_uid=$(id -u)
  owner_uid=$(mim_rtb_stat_owner_uid "$path") || mim_rtb_fail "Unable to inspect $label ownership"
  [[ "$owner_uid" == "$current_uid" ]] || mim_rtb_fail "$label must be owned by the current user"

  mode=$(mim_rtb_stat_mode "$path") || mim_rtb_fail "Unable to inspect $label permissions"
  [[ "$mode" == "600" ]] || mim_rtb_fail "$label must use mode 0600"
}

mim_rtb_default_input_file() {
  local script_dir=$1
  printf '%s/runtime-bootstrap.private.json' "$script_dir"
}

mim_rtb_default_state_dir() {
  local script_dir=$1
  printf '%s/.state' "$script_dir"
}

mim_rtb_resolve_physical_dir() {
  local dir=$1
  (cd "$dir" && pwd -P)
}

mim_rtb_ensure_state_dir() {
  local state_dir=$1
  [[ ! -L "$state_dir" ]] || mim_rtb_fail ".state directory must not be a symlink"
  mkdir -p "$state_dir"
  chmod 700 "$state_dir"
}

mim_rtb_safe_plan_filename() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9._-]*\.json$ ]]
}

mim_rtb_assert_plan_create_path() {
  local script_dir=$1
  local path=$2
  local state_dir expected_parent actual_parent base_name

  state_dir=$(mim_rtb_default_state_dir "$script_dir")
  mim_rtb_ensure_state_dir "$state_dir"
  base_name=$(basename "$path")
  mim_rtb_safe_plan_filename "$base_name" || mim_rtb_fail "Plan filename is invalid"
  expected_parent=$(mim_rtb_resolve_physical_dir "$state_dir")
  actual_parent=$(mim_rtb_resolve_physical_dir "$(dirname "$path")" 2>/dev/null || true)
  [[ "$actual_parent" == "$expected_parent" ]] || mim_rtb_fail "Plan output must stay inside the literal .state directory"
  [[ ! -L "$path" ]] || mim_rtb_fail "Plan output target must not be a symlink"
  [[ ! -e "$path" ]] || mim_rtb_fail "Refusing to overwrite existing reviewed plan"
  [[ ! -e "$path.sha256" ]] || mim_rtb_fail "Refusing to overwrite existing reviewed plan"
}

mim_rtb_assert_plan_read_path() {
  local script_dir=$1
  local path=$2
  local state_dir expected_parent actual_parent

  state_dir=$(mim_rtb_default_state_dir "$script_dir")
  mim_rtb_ensure_state_dir "$state_dir"
  expected_parent=$(mim_rtb_resolve_physical_dir "$state_dir")
  actual_parent=$(mim_rtb_resolve_physical_dir "$(dirname "$path")" 2>/dev/null || true)
  [[ "$actual_parent" == "$expected_parent" ]] || mim_rtb_fail "Plan file must stay inside the literal .state directory"
  mim_rtb_assert_private_regular_file "Plan file" "$path"
  mim_rtb_assert_private_regular_file "Plan hash file" "$path.sha256"
}

mim_rtb_sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    LC_ALL=C shasum -a 256 "$1" | awk '{print $1}'
    return
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
    return
  fi
  mim_rtb_fail "A SHA-256 tool is required"
}

mim_rtb_now_epoch() {
  date +%s
}

mim_rtb_write_hash_sidecar() {
  local path=$1
  printf '%s  %s\n' "$(mim_rtb_sha256_file "$path")" "$(basename "$path")" >"$path.sha256"
  chmod 600 "$path.sha256"
}

mim_rtb_validate_plan_hash_and_age() {
  local plan_path=$1
  local expected_hash actual_hash generated_at expires_at now_epoch

  expected_hash=$(awk '{print $1}' "$plan_path.sha256")
  actual_hash=$(mim_rtb_sha256_file "$plan_path")
  [[ "$expected_hash" == "$actual_hash" ]] || mim_rtb_fail "Plan hash verification failed"

  generated_at=$(python3 - "$plan_path" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["generated_at_epoch"])
PY
)
  expires_at=$(python3 - "$plan_path" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["expires_at_epoch"])
PY
)
  [[ "$generated_at" =~ ^[0-9]+$ ]] || mim_rtb_fail "Plan file does not match the expected reviewed contract"
  [[ "$expires_at" =~ ^[0-9]+$ ]] || mim_rtb_fail "Plan file does not match the expected reviewed contract"
  now_epoch=$(mim_rtb_now_epoch)
  (( generated_at <= now_epoch )) || mim_rtb_fail "Plan generated_at cannot be in the future"
  (( expires_at - generated_at == MIM_RTB_PLAN_MAX_AGE_SECONDS )) || mim_rtb_fail "Plan expiry must be exactly 1800 seconds after generation"
  (( now_epoch <= expires_at )) || mim_rtb_fail "Plan is older than 30 minutes"
}

mim_rtb_contract_path() {
  local script_dir=$1
  printf '%s/bootstrap_contract.py' "$script_dir"
}

mim_rtb_gcloud_capture() {
  local description=$1
  shift
  local output
  if ! output=$(gcloud "$@" 2>/dev/null); then
    mim_rtb_fail "$description"
  fi
  printf '%s' "$output"
}

mim_rtb_gcloud_optional_output() {
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

  if grep -Eq 'NOT_FOUND:|not found|Resource not found' "$stderr_file"; then
    rm -f -- "$stderr_file"
    : >"$output_file"
    printf 'missing'
    return
  fi

  rm -f -- "$stderr_file"
  mim_rtb_fail "$description"
}
