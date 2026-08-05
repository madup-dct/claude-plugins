#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PREFLIGHT_SCRIPT="$SCRIPT_DIR/preflight.sh"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

OPERATOR_EMAIL=operator.test@madup.com
PROJECT_ID=mim-prod-123456
ORGANIZATION_ID=123456789012
BILLING_ACCOUNT_ID=ABCDEF-123456-7890AB
OTHER_PROJECT_ID=other-prod-654321

GCLOUD_LOG="$TMP_DIR/gcloud.log"
GCLOUD_STUB_DIR="$TMP_DIR/bin"
mkdir -p "$GCLOUD_STUB_DIR"

cat >"$GCLOUD_STUB_DIR/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"

case "$*" in
  auth\ list\ *"--format=value(account)"*)
    printf '%s\n' "${GCLOUD_ACTIVE_ACCOUNT:-}"
    ;;
  projects\ describe\ *"--format=value(projectId)"*)
    if [[ "${GCLOUD_PROJECT_DESCRIBE_EXIT:-0}" != "0" ]]; then
      printf '%s\n' "${GCLOUD_PROJECT_DESCRIBE_ERROR:-describe failed}" >&2
      exit "${GCLOUD_PROJECT_DESCRIBE_EXIT}"
    fi
    printf '%s\n' "${GCLOUD_PROJECT_DESCRIBE_ID:-}"
    ;;
  projects\ describe\ *"--format=value(parent.type)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_TYPE:-}"
    ;;
  projects\ describe\ *"--format=value(parent.id)"*)
    printf '%s\n' "${GCLOUD_PROJECT_PARENT_ID:-}"
    ;;
  billing\ projects\ describe\ *"--format=value(billingEnabled)"*)
    printf '%s\n' "${GCLOUD_BILLING_ENABLED:-}"
    ;;
  billing\ projects\ describe\ *"--format=value(billingAccountName)"*)
    printf '%s\n' "${GCLOUD_BILLING_ACCOUNT_NAME:-}"
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$GCLOUD_STUB_DIR/gcloud"

FAILURES=0

write_private_file() {
  local path=$1
  local body=$2
  printf '%s' "$body" >"$path"
  chmod 600 "$path"
}

write_valid_config() {
  local config_path=$1
  write_private_file "$config_path" "$(cat <<EOF
MIM_OPERATOR_EMAIL=$OPERATOR_EMAIL
MIM_PROJECT_ID=$PROJECT_ID
MIM_ORGANIZATION_ID=$ORGANIZATION_ID
MIM_BILLING_ACCOUNT_ID=$BILLING_ACCOUNT_ID
EOF
)"
}

assert_output_redacted() {
  local case_name=$1
  local output_path=$2
  local value

  for value in "$OPERATOR_EMAIL" "$PROJECT_ID" "$ORGANIZATION_ID" "$BILLING_ACCOUNT_ID"; do
    if grep -Fq -- "$value" "$output_path"; then
      printf 'FAIL %s: leaked configured value in script output\n' "$case_name" >&2
      cat "$output_path" >&2 || true
      FAILURES=$((FAILURES + 1))
      return
    fi
  done

  if grep -Eq 'top-secret-credential|ya29\.|refresh_token|client_secret' "$output_path"; then
    printf 'FAIL %s: leaked credential-like output\n' "$case_name" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
  fi
}

assert_scoped_gcloud_log() {
  local case_name=$1

  while IFS= read -r line; do
    case "$line" in
      auth\ list\ *)
        [[ "$line" == *"--account=$OPERATOR_EMAIL"* ]] || {
          printf 'FAIL %s: auth list missing --account\n' "$case_name" >&2
          printf '%s\n' "$line" >&2
          FAILURES=$((FAILURES + 1))
        }
        ;;
      *)
        [[ "$line" == *"--account=$OPERATOR_EMAIL"* ]] || {
          printf 'FAIL %s: gcloud call missing --account\n' "$case_name" >&2
          printf '%s\n' "$line" >&2
          FAILURES=$((FAILURES + 1))
        }
        [[ "$line" == *"--project=$PROJECT_ID"* ]] || {
          printf 'FAIL %s: gcloud call missing --project\n' "$case_name" >&2
          printf '%s\n' "$line" >&2
          FAILURES=$((FAILURES + 1))
        }
        ;;
    esac
  done <"$GCLOUD_LOG"
}

run_case() {
  local case_name=$1
  local expected_exit=$2
  local expected_substring=$3
  local config_setup=$4
  local protected_setup=$5
  local output_path="$TMP_DIR/$case_name.out"
  local config_path="$TMP_DIR/$case_name.env"
  local protected_path="$TMP_DIR/$case_name.protected"

  eval "$config_setup"
  eval "$protected_setup"
  : >"$GCLOUD_LOG"

  set +e
  PATH="$GCLOUD_STUB_DIR:$PATH" \
    GCLOUD_LOG="$GCLOUD_LOG" \
    GCLOUD_ACTIVE_ACCOUNT="${GCLOUD_ACTIVE_ACCOUNT:-$OPERATOR_EMAIL}" \
    GCLOUD_PROJECT_DESCRIBE_EXIT="${GCLOUD_PROJECT_DESCRIBE_EXIT:-0}" \
    GCLOUD_PROJECT_DESCRIBE_ERROR="${GCLOUD_PROJECT_DESCRIBE_ERROR:-}" \
    GCLOUD_PROJECT_DESCRIBE_ID="${GCLOUD_PROJECT_DESCRIBE_ID:-$PROJECT_ID}" \
    GCLOUD_PROJECT_PARENT_TYPE="${GCLOUD_PROJECT_PARENT_TYPE:-organization}" \
    GCLOUD_PROJECT_PARENT_ID="${GCLOUD_PROJECT_PARENT_ID:-$ORGANIZATION_ID}" \
    GCLOUD_BILLING_ENABLED="${GCLOUD_BILLING_ENABLED:-True}" \
    GCLOUD_BILLING_ACCOUNT_NAME="${GCLOUD_BILLING_ACCOUNT_NAME:-billingAccounts/$BILLING_ACCOUNT_ID}" \
    MIM_CONFIG_FILE="$config_path" \
    MIM_PROTECTED_PROJECTS_FILE="$protected_path" \
    bash "$PREFLIGHT_SCRIPT" >"$output_path" 2>&1
  local exit_code=$?
  set -e

  if [[ "$exit_code" -ne "$expected_exit" ]]; then
    printf 'FAIL %s: expected exit %s, got %s\n' "$case_name" "$expected_exit" "$exit_code" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  if ! grep -Fq -- "$expected_substring" "$output_path"; then
    printf 'FAIL %s: missing output %s\n' "$case_name" "$expected_substring" >&2
    cat "$output_path" >&2 || true
    FAILURES=$((FAILURES + 1))
    return
  fi

  assert_output_redacted "$case_name" "$output_path"
  printf 'PASS %s\n' "$case_name"
}

VALID_CONFIG_SETUP='write_valid_config "$config_path"'
VALID_PROTECTED_SETUP='write_private_file "$protected_path" "$OTHER_PROJECT_ID"$'"'"'\n'"'"''

run_case accepts_valid_operator_boundary 0 "Preflight checks passed." \
  "$VALID_CONFIG_SETUP" \
  "$VALID_PROTECTED_SETUP"
assert_scoped_gcloud_log accepts_valid_operator_boundary

run_case rejects_config_mode_0644 1 "Config file must use mode 0600" \
  'write_valid_config "$config_path"; chmod 644 "$config_path"' \
  "$VALID_PROTECTED_SETUP"
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_config_mode_0644: should reject config before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}

run_case rejects_config_symlink 1 "Config file must not be a symlink" \
  'write_valid_config "$TMP_DIR/source.env"; ln -sf "$TMP_DIR/source.env" "$config_path"' \
  "$VALID_PROTECTED_SETUP"
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_config_symlink: should reject config before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}

run_case rejects_config_unreadable 1 "Config file is missing or unreadable" \
  'write_valid_config "$config_path"; chmod 000 "$config_path"' \
  "$VALID_PROTECTED_SETUP"
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_config_unreadable: should reject config before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}
chmod 600 "$TMP_DIR/rejects_config_unreadable.env" 2>/dev/null || true

run_case rejects_protected_mode_0644 1 "Protected project file must use mode 0600" \
  "$VALID_CONFIG_SETUP" \
  'write_private_file "$protected_path" "$OTHER_PROJECT_ID"$'"'"'\n'"'"'; chmod 644 "$protected_path"'
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_protected_mode_0644: should reject protected file before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}

run_case rejects_protected_symlink 1 "Protected project file must not be a symlink" \
  "$VALID_CONFIG_SETUP" \
  'write_private_file "$TMP_DIR/source.protected" "$OTHER_PROJECT_ID"$'"'"'\n'"'"'; ln -sf "$TMP_DIR/source.protected" "$protected_path"'
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_protected_symlink: should reject protected file before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}

run_case rejects_protected_unreadable 1 "Protected project file is missing or unreadable" \
  "$VALID_CONFIG_SETUP" \
  'write_private_file "$protected_path" "$OTHER_PROJECT_ID"$'"'"'\n'"'"'; chmod 000 "$protected_path"'
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_protected_unreadable: should reject protected file before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}
chmod 600 "$TMP_DIR/rejects_protected_unreadable.protected" 2>/dev/null || true

run_case rejects_legacy_key_before_gcloud 1 "Deprecated config key: MIM_REGION" \
  'write_private_file "$config_path" "$(cat <<EOF
MIM_OPERATOR_EMAIL='"$OPERATOR_EMAIL"'
MIM_PROJECT_ID='"$PROJECT_ID"'
MIM_ORGANIZATION_ID='"$ORGANIZATION_ID"'
MIM_BILLING_ACCOUNT_ID='"$BILLING_ACCOUNT_ID"'
MIM_REGION=asia-northeast3
EOF
)"' \
  "$VALID_PROTECTED_SETUP"
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_legacy_key_before_gcloud: should reject config before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}

run_case rejects_selected_project_from_protected_file 1 "Selected project is protected" \
  "$VALID_CONFIG_SETUP" \
  'write_private_file "$protected_path" "$PROJECT_ID"$'"'"'\n'"'"''
[[ -s "$GCLOUD_LOG" ]] && {
  printf 'FAIL rejects_selected_project_from_protected_file: should reject protected project before gcloud calls\n' >&2
  FAILURES=$((FAILURES + 1))
}

GCLOUD_ACTIVE_ACCOUNT=wrong.test@madup.com \
run_case rejects_wrong_active_account 1 "Active gcloud account does not match the configured operator" \
  "$VALID_CONFIG_SETUP" \
  "$VALID_PROTECTED_SETUP"

GCLOUD_PROJECT_PARENT_ID=999999999999 \
run_case rejects_wrong_organization 1 "Project organization mismatch" \
  "$VALID_CONFIG_SETUP" \
  "$VALID_PROTECTED_SETUP"

GCLOUD_BILLING_ACCOUNT_NAME=billingAccounts/OTHERAB-654321-BA0987 \
run_case rejects_wrong_billing_account 1 "Billing account mismatch" \
  "$VALID_CONFIG_SETUP" \
  "$VALID_PROTECTED_SETUP"

GCLOUD_PROJECT_DESCRIBE_EXIT=2 \
GCLOUD_PROJECT_DESCRIBE_ERROR=PERMISSION_DENIED \
run_case rejects_project_describe_error 1 "Unable to describe the configured project" \
  "$VALID_CONFIG_SETUP" \
  "$VALID_PROTECTED_SETUP"

if [[ "$FAILURES" -ne 0 ]]; then
  exit 1
fi
