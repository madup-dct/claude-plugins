#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/test_common.sh"

PREPARE_SCRIPT="$SCRIPT_DIR/prepare_input.sh"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

FAILURES=0

assert_mode_600() {
  local path=$1
  local mode
  mode=$(stat -f '%Lp' "$path" 2>/dev/null || stat -c '%a' "$path")
  [[ "$mode" == "600" ]] || {
    printf 'FAIL prepare_mode: expected 600, got %s\n' "$mode" >&2
    FAILURES=$((FAILURES + 1))
  }
}

run_case() {
  local case_name=$1
  local expected_exit=$2
  local expected_text=$3
  shift 3
  local output="$TMP_DIR/$case_name.out"

  set +e
  bash "$PREPARE_SCRIPT" "$@" >"$output" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  rtb_assert_contains "$output" "$expected_text" "$case_name" || FAILURES=$((FAILURES + 1))
}

OUTPUT_FILE="$TMP_DIR/bootstrap.json"
run_case creates_private_input 0 "Wrote bootstrap template" --output "$OUTPUT_FILE"
[[ -f "$OUTPUT_FILE" ]] || {
  printf 'FAIL creates_private_input: missing output file\n' >&2
  FAILURES=$((FAILURES + 1))
}
assert_mode_600 "$OUTPUT_FILE"
rtb_assert_contains "$OUTPUT_FILE" '"project_id": "mim-prod-123456"' creates_private_input || FAILURES=$((FAILURES + 1))
rtb_assert_contains "$OUTPUT_FILE" 'versions/123' creates_private_input || FAILURES=$((FAILURES + 1))
rtb_assert_not_contains "$OUTPUT_FILE" 'xoxb-' creates_private_input || FAILURES=$((FAILURES + 1))
rtb_assert_not_contains "$OUTPUT_FILE" '"slack"' creates_private_input || FAILURES=$((FAILURES + 1))

run_case rejects_overwrite 1 "Refusing to overwrite existing file" --output "$OUTPUT_FILE"
run_case rejects_missing_output 1 "Usage:"
run_case rejects_unknown_flag 1 "Unknown argument: --config" --config "$OUTPUT_FILE"
run_case rejects_positional_arg 1 "Positional arguments are not supported" "$OUTPUT_FILE"

if [[ "$FAILURES" -ne 0 ]]; then
  printf 'FAIL: %s prepare_input assertions failed\n' "$FAILURES" >&2
  exit 1
fi

printf 'PASS test_prepare_input.sh\n'
