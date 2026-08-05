#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/test_common.sh"

PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

INPUT_FILE="$TMP_DIR/input.json"
STATE_DIR="$SCRIPT_DIR/.state"
PLAN_FILE="$STATE_DIR/test-runtime-bootstrap-plan.json"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
mkdir -p "$STATE_DIR"
rm -f "$PLAN_FILE" "$PLAN_FILE.sha256"

rtb_write_valid_input "$INPUT_FILE"

STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"

cat >"$STUB_BIN/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"

case "$*" in
  secrets\ describe\ mim-runtime-bootstrap\ *"--format=json"*)
    if [[ "${RTB_SECRET_EXISTS:-true}" != "true" ]]; then
      printf 'NOT_FOUND: secret missing\n' >&2
      exit 1
    fi
    cat "${RTB_SECRET_METADATA_FILE:?}"
    ;;
  secrets\ versions\ list\ *"--format=json"*)
    if [[ "${RTB_SECRET_EXISTS:-true}" != "true" ]]; then
      printf 'NOT_FOUND: secret missing\n' >&2
      exit 1
    fi
    cat "${RTB_VERSIONS_METADATA_FILE:?}"
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/gcloud"

cat >"$TMP_DIR/secret.json" <<'EOF'
{"name":"projects/mim-prod-123456/secrets/mim-runtime-bootstrap","etag":"etag-secret-1","replication":{"automatic":{}}}
EOF
cat >"$TMP_DIR/versions.json" <<'EOF'
[{"name":"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7","state":"ENABLED","etag":"etag-version-7"}]
EOF

OUTPUT_FILE="$TMP_DIR/plan.out"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$TMP_DIR/secret.json" \
RTB_VERSIONS_METADATA_FILE="$TMP_DIR/versions.json" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_FILE" >"$OUTPUT_FILE" 2>&1

[[ -f "$PLAN_FILE" ]] || {
  printf 'FAIL plan_success: plan file missing\n' >&2
  exit 1
}
[[ -f "$PLAN_FILE.sha256" ]] || {
  printf 'FAIL plan_success: hash sidecar missing\n' >&2
  exit 1
}

rtb_assert_contains "$OUTPUT_FILE" "Wrote reviewed plan" plan_success
rtb_assert_not_contains "$OUTPUT_FILE" 'deploy-key-202608' plan_success
rtb_assert_not_contains "$OUTPUT_FILE" "$(head -n 1 "$INPUT_FILE")" plan_success
rtb_assert_contains "$PLAN_FILE" '"target_secret_name":"mim-runtime-bootstrap"' plan_success
rtb_assert_contains "$PLAN_FILE" '"status":"ready"' plan_success
rtb_assert_contains "$PLAN_FILE" '"input_sha256":"' plan_success
rtb_assert_contains "$PLAN_FILE" '"latest_enabled_version":"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7"' plan_success
rtb_assert_not_contains "$PLAN_FILE" '"payload"' plan_success
rtb_assert_not_contains "$PLAN_FILE" 'latest"' plan_success
rtb_assert_contains "$GCLOUD_LOG" "--account=$RTB_OPERATOR_EMAIL" plan_success
rtb_assert_not_contains "$GCLOUD_LOG" "$RTB_PERSONAL_EMAIL" plan_success

rm -f "$PLAN_FILE" "$PLAN_FILE.sha256"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_EXISTS=false \
RTB_SECRET_METADATA_FILE="$TMP_DIR/secret.json" \
RTB_VERSIONS_METADATA_FILE="$TMP_DIR/versions.json" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_FILE" >"$OUTPUT_FILE" 2>&1 || true
rtb_assert_contains "$PLAN_FILE" '"status":"blocked"' plan_blocked
rtb_assert_contains "$PLAN_FILE" 'central infra must create the secret container' plan_blocked

printf 'PASS test_plan.sh\n'
