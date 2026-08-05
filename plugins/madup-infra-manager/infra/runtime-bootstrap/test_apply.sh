#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/test_common.sh"

PLAN_SCRIPT="$SCRIPT_DIR/plan.sh"
APPLY_SCRIPT="$SCRIPT_DIR/apply.sh"
TMP_DIR=$(mktemp -d)
STATE_DIR="$SCRIPT_DIR/.state"
trap 'rm -rf "$TMP_DIR"; rm -f "$STATE_DIR"/test-runtime-bootstrap-apply*.json "$STATE_DIR"/test-runtime-bootstrap-apply*.json.sha256' EXIT
mkdir -p "$STATE_DIR"

INPUT_FILE="$TMP_DIR/input.json"
PLAN_FILE="$STATE_DIR/test-runtime-bootstrap-apply.json"
GCLOUD_LOG="$TMP_DIR/gcloud.log"
SECRET_METADATA_FILE="$TMP_DIR/secret.json"
VERSIONS_METADATA_FILE="$TMP_DIR/versions.json"
PAYLOAD_SHA_FILE="$TMP_DIR/payload-sha.txt"
CURRENT_VERSION_FILE="$TMP_DIR/current-version.txt"
OUTPUT_FILE="$TMP_DIR/output.txt"

rtb_write_valid_input "$INPUT_FILE"

cat >"$SECRET_METADATA_FILE" <<'EOF'
{"name":"projects/mim-prod-123456/secrets/mim-runtime-bootstrap","etag":"etag-secret-1","replication":{"automatic":{}}}
EOF
cat >"$VERSIONS_METADATA_FILE" <<'EOF'
[{"name":"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7","state":"ENABLED","etag":"etag-version-7"}]
EOF
printf '%s' "7" >"$CURRENT_VERSION_FILE"

STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"
cat >"$STUB_BIN/gcloud" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${GCLOUD_LOG:?}"

if [[ "$*" == *"--impersonate-service-account="* ]]; then
  :
elif [[ "$*" == secrets\ versions\ add\ * ]]; then
  printf 'missing impersonation\n' >&2
  exit 98
fi

case "$*" in
  secrets\ describe\ mim-runtime-bootstrap\ *"--format=json"*)
    cat "${RTB_SECRET_METADATA_FILE:?}"
    ;;
  secrets\ versions\ list\ *"--format=json"*)
    cat "${RTB_VERSIONS_METADATA_FILE:?}"
    ;;
  secrets\ versions\ add\ mim-runtime-bootstrap\ *"--format=value(name)"*)
    data_file=
    for arg in "$@"; do
      case "$arg" in
        --data-file=*)
          data_file=${arg#--data-file=}
          ;;
      esac
    done
    [[ -n "$data_file" ]] || {
      printf 'missing data file\n' >&2
      exit 97
    }
    python3 - "$data_file" "${RTB_PAYLOAD_SHA_FILE:?}" "${RTB_VERSIONS_METADATA_FILE:?}" "${RTB_CURRENT_VERSION_FILE:?}" <<'PY'
import json
import hashlib
import sys
from pathlib import Path

payload_path = Path(sys.argv[1])
sha_path = Path(sys.argv[2])
versions_path = Path(sys.argv[3])
current_version_path = Path(sys.argv[4])
payload = payload_path.read_bytes()
json.loads(payload.decode("utf-8"))
sha_path.write_text(hashlib.sha256(payload).hexdigest(), encoding="utf-8")
current = int(current_version_path.read_text(encoding="utf-8").strip())
new_version = current + 1
versions = json.loads(versions_path.read_text(encoding="utf-8"))
versions.append(
    {
        "name": f"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/{new_version}",
        "state": "ENABLED",
        "etag": f"etag-version-{new_version}",
    }
)
versions_path.write_text(json.dumps(versions, separators=(",", ":")), encoding="utf-8")
current_version_path.write_text(str(new_version), encoding="utf-8")
print(f"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/{new_version}")
PY
    ;;
  secrets\ versions\ describe\ *"--format=json"*)
    python3 - "${RTB_CURRENT_VERSION_FILE:?}" <<'PY'
import json
import sys
from pathlib import Path

version = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
print(json.dumps({
    "name": f"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/{version}",
    "state": "ENABLED",
}))
PY
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$STUB_BIN/gcloud"

PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_FILE" >/dev/null

PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_FILE" >"$OUTPUT_FILE" 2>&1

rtb_assert_contains "$OUTPUT_FILE" "$RTB_BOOTSTRAP_VERSION_8" apply_success
[[ "$(cat "$PAYLOAD_SHA_FILE")" == "$(python3 - "$INPUT_FILE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
print(hashlib.sha256(encoded).hexdigest())
PY
)" ]] || {
  printf 'FAIL apply_success: payload SHA mismatch\n' >&2
  exit 1
}
rtb_assert_contains "$GCLOUD_LOG" "--impersonate-service-account=$RTB_RELEASE_SA" apply_success
rtb_assert_contains "$GCLOUD_LOG" "--account=$RTB_OPERATOR_EMAIL" apply_success
rtb_assert_not_contains "$GCLOUD_LOG" "$RTB_PERSONAL_EMAIL" apply_success
rtb_assert_not_contains "$OUTPUT_FILE" 'deploy-key-202608' apply_success

set +e
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_FILE" >"$OUTPUT_FILE" 2>&1
duplicate_exit=$?
set -e
[[ "$duplicate_exit" -ne 0 ]] || {
  printf 'FAIL duplicate_apply: expected failure\n' >&2
  exit 1
}
rtb_assert_contains "$OUTPUT_FILE" "Discovery drift detected" duplicate_apply

rm -f "$PLAN_FILE" "$PLAN_FILE.sha256"
printf '%s' "7" >"$CURRENT_VERSION_FILE"
cat >"$VERSIONS_METADATA_FILE" <<'EOF'
[{"name":"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7","state":"ENABLED","etag":"etag-version-7"}]
EOF
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_FILE" >/dev/null
python3 - "$PLAN_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["status"] = "tampered"
path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
PY
set +e
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_FILE" >"$OUTPUT_FILE" 2>&1
tamper_exit=$?
set -e
[[ "$tamper_exit" -ne 0 ]] || {
  printf 'FAIL tamper_rejected: expected failure\n' >&2
  exit 1
}
rtb_assert_contains "$OUTPUT_FILE" "Plan hash verification failed" tamper_rejected

rm -f "$PLAN_FILE" "$PLAN_FILE.sha256"
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_FILE" >/dev/null
python3 - "$VERSIONS_METADATA_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
versions = json.loads(path.read_text(encoding="utf-8"))
versions.append({
    "name": "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/99",
    "state": "ENABLED",
    "etag": "etag-version-99",
})
path.write_text(json.dumps(versions, separators=(",", ":")), encoding="utf-8")
PY
set +e
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_FILE" >"$OUTPUT_FILE" 2>&1
drift_exit=$?
set -e
[[ "$drift_exit" -ne 0 ]] || {
  printf 'FAIL drift_rejected: expected failure\n' >&2
  exit 1
}
rtb_assert_contains "$OUTPUT_FILE" "Discovery drift detected" drift_rejected

rm -f "$PLAN_FILE" "$PLAN_FILE.sha256"
cat >"$VERSIONS_METADATA_FILE" <<'EOF'
[{"name":"projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7","state":"ENABLED","etag":"etag-version-7"}]
EOF
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$PLAN_SCRIPT" --plan --out "$PLAN_FILE" >/dev/null
printf '\n' >>"$INPUT_FILE"
set +e
PATH="$STUB_BIN:$PATH" \
GCLOUD_LOG="$GCLOUD_LOG" \
RTB_SECRET_METADATA_FILE="$SECRET_METADATA_FILE" \
RTB_VERSIONS_METADATA_FILE="$VERSIONS_METADATA_FILE" \
RTB_PAYLOAD_SHA_FILE="$PAYLOAD_SHA_FILE" \
RTB_CURRENT_VERSION_FILE="$CURRENT_VERSION_FILE" \
MIM_RUNTIME_BOOTSTRAP_INPUT_FILE="$INPUT_FILE" \
bash "$APPLY_SCRIPT" --apply --plan-file "$PLAN_FILE" >"$OUTPUT_FILE" 2>&1
input_exit=$?
set -e
[[ "$input_exit" -ne 0 ]] || {
  printf 'FAIL input_hash_rejected: expected failure\n' >&2
  exit 1
}
rtb_assert_contains "$OUTPUT_FILE" "Input SHA-256 does not match the reviewed plan" input_hash_rejected

printf 'PASS test_apply.sh\n'
