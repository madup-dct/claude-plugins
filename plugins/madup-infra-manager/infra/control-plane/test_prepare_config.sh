#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PREPARE_SCRIPT="$SCRIPT_DIR/prepare_config.sh"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

FAILURES=0

assert_contains() {
  local file=$1
  local expected=$2
  local case_name=$3

  if ! grep -Fq -- "$expected" "$file"; then
    printf 'FAIL %s: missing %s\n' "$case_name" "$expected" >&2
    cat "$file" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
}

run_case() {
  local case_name=$1
  local expected_exit=$2
  local expected_output=$3
  shift 3

  local output_path="$TMP_DIR/$case_name.out"

  set +e
  bash "$PREPARE_SCRIPT" "$@" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  assert_contains "$output_path" "$expected_output" "$case_name"
  printf 'PASS %s\n' "$case_name"
}

CONFIG_PATH="$TMP_DIR/generated.env"

run_case creates_private_config 0 "Wrote config template" --output "$CONFIG_PATH"
[[ -f "$CONFIG_PATH" ]] || {
  printf 'FAIL creates_private_config: config file missing\n' >&2
  FAILURES=$((FAILURES + 1))
}
[[ "$(stat -f '%Lp' "$CONFIG_PATH" 2>/dev/null || stat -c '%a' "$CONFIG_PATH")" == "600" ]] || {
  printf 'FAIL creates_private_config: mode must be 0600\n' >&2
  FAILURES=$((FAILURES + 1))
}
assert_contains "$CONFIG_PATH" "MIM_OPERATOR_EMAIL=<madup.com-email>" creates_private_config
assert_contains "$CONFIG_PATH" "MIM_PROJECT_ID=<gcp-project-id>" creates_private_config
assert_contains "$CONFIG_PATH" "MIM_CLOUDFLARE_ACCOUNT_ID=<required-cloudflare-account-id>" creates_private_config
assert_contains "$CONFIG_PATH" "MIM_GITHUB_REPOSITORY_IDS=<required-comma-separated-repository-ids>" creates_private_config
assert_contains "$CONFIG_PATH" "MIM_SLACK_APPROVED_WORKSPACE_IDS=<required-comma-separated-workspace-ids>" creates_private_config

run_case rejects_overwrite 1 "Refusing to overwrite existing file" --output "$CONFIG_PATH"
run_case rejects_missing_output 1 "Usage:"
run_case rejects_unknown_flag 1 "Unknown argument: --config" --config "$CONFIG_PATH"
run_case rejects_positional_arg 1 "Positional arguments are not supported" "$CONFIG_PATH"
run_case rejects_missing_output_value 1 "Missing value for --output" --output

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s prepare_config assertions failed\n' "$FAILURES" >&2
  exit 1
fi

printf 'PASS test_prepare_config.sh\n'
